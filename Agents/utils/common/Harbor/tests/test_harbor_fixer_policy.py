"""Tests for Harbor Fixer execution policy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import (
    FixerTestCase,
    PolicyInvoker,
    SequenceInvoker,
    make_fix_plan,
    write_json,
)
from harbor_fixer.policy import evaluate_t1, load_user_rules, run_policy_preflight


class HarborFixerPolicyTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _run_preflight(
        self,
        plan: dict,
        invoker: PolicyInvoker | SequenceInvoker | None,
        *,
        user_rules_path: Path | None = None,
        writable_roots: list[Path] | None = None,
    ) -> dict:
        return run_policy_preflight(
            plan,
            self.workspace,
            self.root / "policy",
            invoker,
            user_rules_path=user_rules_path,
            writable_roots=writable_roots,
        )

    def test_t1_allows_narrow_reads_and_denies_destructive_commands(self) -> None:
        for command in (
            "docker ps --all",
            "git status --short",
            "cat pyproject.toml | grep dependency",
        ):
            with self.subTest(command=command):
                decision = evaluate_t1(command, [])
                self.assertEqual(
                    (decision["tier"], decision["decision"]), ("T1", "allow")
                )

        for command in (
            "rm -rf build",
            "sudo -u root rm -f /tmp/item",
            "bash -lc 'rm -rf build'",
            "echo $(rm -f marker)",
            "printf '%s\\n' marker | xargs rm -f",
            "docker exec fixture rm -f /tmp/item",
            "find . -delete",
        ):
            with self.subTest(command=command):
                decision = evaluate_t1(command, [])
                self.assertEqual(
                    (decision["tier"], decision["decision"]), ("T1", "deny")
                )

        for command in (
            "diff --output=change.patch old new",
            "git diff --output=change.patch",
        ):
            with self.subTest(command=command):
                self.assertIsNone(evaluate_t1(command, []))

    def test_user_deny_precedes_allow_and_builtin_deny_cannot_be_overridden(
        self,
    ) -> None:
        rules_path = self.root / "rules.json"
        write_json(
            rules_path,
            {
                "schema_version": 1,
                "kind": "harbor_fixer_policy_rules",
                "deny": [
                    {
                        "rule_id": "deny-docker-ps",
                        "pattern": ["docker", "ps"],
                        "match": "prefix",
                    }
                ],
                "allow": [
                    {
                        "rule_id": "allow-docker-ps",
                        "pattern": ["docker", "ps"],
                        "match": "prefix",
                    },
                    {
                        "rule_id": "attempt-allow-rm",
                        "pattern": ["rm", "-rf"],
                        "match": "prefix",
                    },
                    {
                        "rule_id": "allow-pytest",
                        "pattern": ["pytest", "-q"],
                        "match": "prefix",
                    },
                ],
            },
        )
        rules, source = load_user_rules(rules_path)
        self.assertEqual(source, str(rules_path))
        self.assertEqual(
            evaluate_t1("docker ps -a", rules)["rule_id"], "deny-docker-ps"
        )
        self.assertEqual(
            evaluate_t1("rm -rf disposable", rules)["source"],
            "builtin_rule",
        )
        self.assertEqual(
            evaluate_t1("pytest -q tests/unit", rules)["decision"],
            "allow",
        )

    def test_preflight_routes_inside_writes_to_t2_and_other_operations_to_t3(
        self,
    ) -> None:
        plan = make_fix_plan()
        plan["plans"][0]["commands"] = [
            {
                "command_id": "inside-write",
                "cwd": ".",
                "command": "printf enabled > daemon.json",
                "purpose": "Update the fixture daemon configuration.",
                "expected_effect": "The daemon configuration is enabled.",
            },
            {
                "command_id": "docker-build",
                "cwd": ".",
                "command": "docker build -t fixture .",
                "purpose": "Build the repaired benchmark image.",
                "expected_effect": "The repaired image exists.",
            },
        ]
        invoker = PolicyInvoker()
        result = self._run_preflight(plan, invoker)

        self.assertEqual(result["status"], "allowed")
        self.assertEqual(
            [decision["tier"] for decision in result["decisions"]],
            ["T2", "T3"],
        )
        self.assertEqual(
            [record[1]["tier"] for record in invoker.records],
            ["T2", "T3"],
        )

    def test_symlink_escape_routes_write_to_t3(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        plan = make_fix_plan()
        plan["plans"][0]["commands"][0].update(
            {
                "command": "touch escape/config.json",
                "purpose": "Write through a symlink.",
            }
        )
        invoker = PolicyInvoker()

        result = self._run_preflight(plan, invoker)

        decision = result["decisions"][0]
        self.assertEqual(decision["tier"], "T3")
        self.assertFalse(
            decision["path_analysis"]["write_targets"][0]["inside_writable_roots"]
        )

    def test_additional_write_root_routes_outside_workspace_write_to_t2(self) -> None:
        config_root = self.root / "daemon-config"
        config_root.mkdir()
        plan = make_fix_plan()
        plan["plans"][0]["commands"][0]["command"] = (
            f"touch {config_root / 'daemon.json'}"
        )
        invoker = PolicyInvoker()

        result = self._run_preflight(
            plan,
            invoker,
            writable_roots=[config_root],
        )

        self.assertEqual(result["decisions"][0]["tier"], "T2")
        self.assertIn(str(config_root.resolve()), result["writable_roots"])

    def test_policy_agent_invalid_output_retries_then_fails_closed(self) -> None:
        plan = make_fix_plan()
        plan["plans"][0]["commands"][0]["command"] = "docker build ."
        invoker = SequenceInvoker(["not-json", json.dumps({"decision": "allow"})])

        result = self._run_preflight(plan, invoker)

        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result["decisions"][0]["reason_code"], "policy_agent_failed_closed"
        )
        self.assertEqual(invoker.calls, 2)
        self.assertIn("Validation retry:", invoker.records[1][0])

    def test_invalid_user_rules_fail_closed_before_t1_allow(self) -> None:
        result = self._run_preflight(
            make_fix_plan(),
            None,
            user_rules_path=self.root / "missing-rules.json",
        )

        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result["decisions"][0]["reason_code"],
            "policy_configuration_error",
        )


if __name__ == "__main__":
    unittest.main()
