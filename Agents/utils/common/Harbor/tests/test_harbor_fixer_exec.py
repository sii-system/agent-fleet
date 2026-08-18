"""Tests for Harbor Fixer execution."""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, PolicyInvoker, make_fix_plan, write_json
from harbor_fixer import executor
from harbor_fixer.executor import build_exec_input, run_fix_exec


def _command(action_id: str, script: str) -> dict:
    return {
        "action_id": action_id,
        "action_type": "command",
        "cwd": ".",
        "executable": sys.executable,
        "arguments": ["-c", script],
        "purpose": "fixture",
        "expected_effect": "fixture",
    }


def _file_edit(path: str) -> dict:
    return {
        "action_id": "edit-001",
        "action_type": "file_edit",
        "cwd": ".",
        "path": path,
        "edit": {
            "kind": "replace_text",
            "old_text": "false",
            "new_text": "true",
            "expected_replacements": 1,
        },
        "purpose": "enable fixture",
        "expected_effect": "fixture is enabled",
    }


class HarborFixerExecTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _run(
        self,
        actions: list[dict],
        name: str,
        *,
        plan: dict | None = None,
        roots: list[Path] | None = None,
        timeout: float = 300,
        summary_limit: int = 4000,
    ) -> dict:
        plan = plan or make_fix_plan()
        plan["plans"][0]["actions"] = actions
        plan_path = self.root / f"{name}-plan.json"
        write_json(plan_path, plan)
        return run_fix_exec(
            build_exec_input(plan_path, self.workspace),
            self.root / name,
            policy_invoker=PolicyInvoker(),
            policy_write_roots=roots,
            execution_timeout_seconds=timeout,
            summary_limit=summary_limit,
        )

    def test_order_logs_and_failure_boundaries(self) -> None:
        actions = [
            _command("one", "open('order.txt', 'a').write('1')"),
            _command("fail", "import sys; open('order.txt', 'a').write('2'); sys.exit(7)"),
            _command("skip", "open('order.txt', 'a').write('X')"),
        ]
        plan = make_fix_plan()
        later = copy.deepcopy(plan["plans"][0])
        later["plan_id"] = "fix-002"
        later["task_list"][0].update({"task_index": "2", "task_name": "task-2"})
        later["actions"] = [_command("three", "open('order.txt', 'a').write('3')")]
        later["verification_hint"]["target_task_indexes"] = ["2"]
        plan["plans"].append(later)

        result = self._run(actions, "order", plan=plan)

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual((self.workspace / "order.txt").read_text(), "123")
        self.assertEqual(result["plans"][0]["actions"][2]["status"], "skipped")

    def test_exact_file_edit(self) -> None:
        target = self.workspace / "daemon.json"
        target.write_text("false\n")
        result = self._run([_file_edit("daemon.json")], "edit", roots=[self.workspace])
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_text(), "true\n")

        pipe = self.workspace / "pipe"
        os.mkfifo(pipe)
        rejected = self._run([_file_edit("pipe")], "fifo", roots=[self.workspace])
        self.assertEqual(rejected["status"], "failed")
        self.assertIn("regular file", rejected["plans"][0]["actions"][0]["stderr_summary"])

    def test_command_environment_is_allowlisted(self) -> None:
        allowed = {"PATH": "/bin", "DOCKER_HOST": "unix:///docker.sock"}
        blocked = {"OPENCODE_CONFIG_CONTENT": "secret", "CUSTOM_HEADERS": "secret"}
        with mock.patch.dict(os.environ, allowed | blocked, clear=True):
            self.assertEqual(executor._command_environment(), allowed)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(executor._command_environment(), {"PATH": os.defpath})

    def test_symlink_swap_cannot_escape_authorized_root(self) -> None:
        allowed = self.workspace / "config"
        outside = self.root / "outside"
        allowed.mkdir()
        outside.mkdir()
        (allowed / "daemon.json").write_text("false\n")
        outside_target = outside / "daemon.json"
        outside_target.write_text("false\n")
        original_open = executor._open_authorized_parent

        def swap_then_open(target: Path, roots: list[Path]) -> tuple[int, str]:
            allowed.rename(self.workspace / "config-original")
            allowed.symlink_to(outside, target_is_directory=True)
            return original_open(target, roots)

        with mock.patch(
            "harbor_fixer.executor._open_authorized_parent",
            side_effect=swap_then_open,
        ):
            result = self._run(
                [_file_edit("config/daemon.json")],
                "race",
                roots=[self.workspace],
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(outside_target.read_text(), "false\n")

    def test_command_timeout(self) -> None:
        timed_out = self._run(
            [_command("timeout", "import time; time.sleep(10)")],
            "timeout",
            timeout=0.05,
        )["plans"][0]["actions"][0]
        self.assertIsNone(timed_out["exit_code"])
        self.assertIn("timed out", timed_out["stderr_summary"])

        limited = self._run(
            [_command("summary", "print('123456')")], "summary", summary_limit=4
        )["plans"][0]["actions"][0]
        self.assertEqual(limited["stdout_summary"], "456\n")

    def test_stale_latest_is_removed_before_preflight(self) -> None:
        output = self.root / "stale"
        output.mkdir()
        latest = output / "exec-result-latest.json"
        latest.write_text("stale")

        with (
            mock.patch(
                "harbor_fixer.executor.run_policy_preflight",
                side_effect=RuntimeError("preflight failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "preflight failed"),
        ):
            self._run([_command("stale", "pass")], "stale")
        self.assertFalse(latest.exists())

    def test_active_action_record_is_present_only_while_command_runs(self) -> None:
        active_path = self.root / "active-record" / "active-action.json"
        script = (
            "import pathlib, time; "
            "time.sleep(0.1); "
            f"assert pathlib.Path({str(active_path)!r}).is_file()"
        )

        result = self._run([_command("active-record", script)], "active-record")

        self.assertEqual(result["status"], "success")
        self.assertFalse(active_path.exists())

    def test_background_action_process_is_stopped_before_completion(self) -> None:
        marker = self.workspace / "background-mutation"
        child_script = (
            "import pathlib, time; "
            "time.sleep(0.5); "
            f"pathlib.Path({str(marker)!r}).write_text('unexpected')"
        )
        parent_script = (
            "import subprocess, sys; "
            f"subprocess.Popen([sys.executable, '-c', {child_script!r}])"
        )

        result = self._run(
            [_command("background", parent_script)],
            "background",
        )
        time.sleep(0.7)

        self.assertEqual(result["status"], "success")
        self.assertFalse(marker.exists())

    def test_exec_input_publish_does_not_follow_existing_symlink(self) -> None:
        output_dir = self.root / "exec-input-symlink"
        plan = make_fix_plan()
        plan_path = self.root / "plan.json"
        write_json(plan_path, plan)
        outside = self.root / "outside-exec-input.txt"
        outside.write_text("outside target", encoding="utf-8")
        output_dir.mkdir()
        (output_dir / "exec-input.json").symlink_to(outside)

        result = run_fix_exec(
            build_exec_input(plan_path, self.workspace),
            output_dir,
            policy_invoker=PolicyInvoker(),
            policy_write_roots=[self.workspace],
        )

        self.assertEqual(result["status"], "success")
        self.assertFalse((output_dir / "exec-input.json").is_symlink())
        self.assertEqual(
            json.loads((output_dir / "exec-input.json").read_text(encoding="utf-8"))[
                "schema_version"
            ],
            1,
        )
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside target")

    def test_action_log_write_does_not_follow_preexisting_paths(self) -> None:
        output_dir = self.root / "action-logs-no-follow"
        plan = make_fix_plan()
        plan["plans"][0]["actions"] = [_command("log-action", "print('ok')")]
        plan_path = self.root / "plan-log.json"
        write_json(plan_path, plan)
        stdout_path, _ = executor._action_log_paths(
            output_dir,
            plan["plans"][0]["plan_id"],
            "log-action",
            0,
        )
        outside = self.root / "outside-action-log.txt"
        outside.write_text("outside log target", encoding="utf-8")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.symlink_to(outside)

        result = run_fix_exec(
            build_exec_input(plan_path, self.workspace),
            output_dir,
            policy_invoker=PolicyInvoker(),
            policy_write_roots=[self.workspace],
        )

        action = result["plans"][0]["actions"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(action["status"], "failed")
        self.assertIn("action log write failed", action["stderr_summary"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside log target")


if __name__ == "__main__":
    unittest.main()
