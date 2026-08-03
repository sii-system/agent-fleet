"""Tests for Harbor Fixer plan."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import (
    FixerTestCase,
    MainInvoker,
    SequenceInvoker,
    SummaryInvoker,
    task_summary_for,
    write_analyzer_fixture,
    write_fixture_pi,
)
from harbor_fixer import plan_generation
from harbor_fixer.analyzer_inputs import build_task_inputs
from harbor_fixer.artifact_io import write_json_atomic
from harbor_fixer.plan_generation import (
    collect_task_summaries,
    request_fix_plan,
    run_plan_generation,
)
from harbor_fixer.validation import (
    ValidationError,
    parse_strict_json_object,
    validate_fix_plan_set,
    validate_task_summary,
)


class HarborFixerPlanTest(FixerTestCase):
    def test_task_summary_retry_identity_and_contract(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root)
        task_input = build_task_inputs(analyzer_dir)[0][0]

        invoker = SequenceInvoker(["not-json", json.dumps(task_summary_for(task_input))])
        summaries, errors = collect_task_summaries([task_input], invoker, self.root / "out")
        self.assertEqual((len(summaries), errors), (1, []))
        retry_prompt = invoker.records[1][0]
        self.assertIn("invalid JSON:", retry_prompt)
        self.assertIn("<previous-output>\nnot-json\n</previous-output>", retry_prompt)

        bad_summary = json.loads(json.dumps(task_summary_for(task_input)))
        bad_summary["analyzer_alignment"]["final_class"] = "infra_fail"
        with self.assertRaises(ValidationError):
            validate_task_summary(bad_summary, expected_input=task_input)

    def test_main_plan_contract_and_generation_error_ownership(self) -> None:
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        summary = task_summary_for(task_input)
        main_input = {
            "source": {"run_id": "fixture"},
            "task_summaries": [summary],
            "generation_errors": [{"stage": "task_subagent", "error": "fixture"}],
        }
        valid_output = MainInvoker().invoke(
            "",
            main_input,
            attempt=1,
            label="main-agent",
        )
        plan = request_fix_plan(
            SequenceInvoker([valid_output]),
            main_input,
            self.root / "main-out",
        )
        self.assertEqual(plan["generation_errors"], main_input["generation_errors"])

        incomplete = json.loads(valid_output)
        del incomplete["plans"][0]["commands"][0]["expected_effect"]
        with self.assertRaisesRegex(ValidationError, "expected_effect"):
            validate_fix_plan_set(
                incomplete,
                expected_source=main_input["source"],
                expected_task_summaries=[summary],
            )

        agent_owned_errors = json.loads(valid_output)
        agent_owned_errors["generation_errors"] = [{"stage": "invented"}]
        with self.assertRaisesRegex(ValidationError, "generation_errors must be empty"):
            request_fix_plan(
                SequenceInvoker([json.dumps(agent_owned_errors)]),
                main_input,
                self.root / "bad-main-out",
                max_attempts=1,
            )

        for comparison, error in (
            ({"analyzer_scopes": ["host"]}, "analyzer scopes"),
            ({"relation": "broader"}, "scope relation"),
        ):
            invalid = json.loads(valid_output)
            invalid["plans"][0]["analyzer_scope_comparison"].update(comparison)
            with self.assertRaisesRegex(ValidationError, error):
                validate_fix_plan_set(invalid, expected_task_summaries=[summary])

        invalid = json.loads(valid_output)
        invalid["plans"][0]["task_list"][0]["attempt_id"] = ""
        with self.assertRaisesRegex(ValidationError, "identity"):
            validate_fix_plan_set(invalid, expected_task_summaries=[summary])

        for value in ("NaN", "Infinity", "1e400"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError,
                "invalid JSON",
            ):
                parse_strict_json_object(f'{{"value": {value}}}')

    def test_summary_size_limits(self) -> None:
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        output = json.dumps(task_summary_for(task_input))

        for limits, error in (
            ({"max_task_summary_chars": 1}, "task_summary_exceeds_size_limit"),
            (
                {"max_task_summaries_chars": 1},
                "task_summaries_exceed_aggregate_size_limit",
            ),
        ):
            summaries, errors = collect_task_summaries(
                [task_input],
                SequenceInvoker([output]),
                self.root / error,
                **limits,
            )
            self.assertEqual((summaries, errors[0]["error"]), ([], error))

    def test_empty_analyzer_output_writes_plan_without_agents(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root, count=0)
        task_invoker = SequenceInvoker([])
        main_invoker = SequenceInvoker([])

        plan = run_plan_generation(
            analyzer_dir,
            self.root / "empty-plan",
            task_invoker,
            main_invoker,
            workspace_root=self.root,
        )

        self.assertEqual(plan["plans"], [])
        self.assertEqual(plan["unplanned_tasks"], [])
        self.assertEqual((task_invoker.calls, main_invoker.calls), (0, 0))
        validate_fix_plan_set(
            plan,
            expected_source=plan["source"],
            expected_task_summaries=[],
        )

    def test_all_task_failures_write_diagnostics_and_fail(self) -> None:
        output_dir = self.root / "failed-plan"
        main_invoker = SequenceInvoker([])

        with self.assertRaisesRegex(ValidationError, "all task subagents failed"):
            run_plan_generation(
                write_analyzer_fixture(self.root),
                output_dir,
                SequenceInvoker(["not-json"]),
                main_invoker,
                workspace_root=self.root,
            )

        plan = json.loads((output_dir / "fix-plan-latest.json").read_text())
        self.assertEqual(len(plan["generation_errors"]), 1)
        self.assertEqual(main_invoker.calls, 0)

    def test_latest_plan_publish_and_failed_regeneration(self) -> None:
        output_dir = self.root / "regeneration"
        latest = output_dir / "fix-plan-latest.json"
        latest.parent.mkdir()
        latest.write_text('{"stale": true}')

        with (
            mock.patch.object(Path, "replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            write_json_atomic(latest, {"stale": False})
        self.assertEqual(json.loads(latest.read_text()), {"stale": True})

        with (
            mock.patch.object(
                plan_generation,
                "request_fix_plan",
                side_effect=ValidationError("main agent failed"),
            ),
            self.assertRaises(ValidationError),
        ):
            run_plan_generation(
                write_analyzer_fixture(self.root),
                output_dir,
                SummaryInvoker(),
                SequenceInvoker([]),
                workspace_root=self.root,
            )

        self.assertFalse(latest.exists())

    def test_plan_generation_and_cli_smoke_write_fix_plan(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root)
        cli_out = self.root / "cli-fixer"
        api_key = "fixture-super-secret-api-key"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--analyzer-output",
                str(analyzer_dir),
                "--output-dir",
                str(cli_out),
                "--pi-bin",
                str(write_fixture_pi(self.root / "fixture_pi.py")),
                "--pi-base-url",
                "https://example.test/v1",
                "--pi-model",
                "fixture-model",
                "--pi-api-key-env",
                "FIXTURE_PI_API_KEY",
                "--max-task-summary-chars",
                "100000",
                "--max-task-summaries-chars",
                "200000",
                "--workspace-root",
                str(self.root),
            ],
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "FIXTURE_PI_API_KEY": api_key},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(
            (cli_out / "fix-plan-latest.json").read_text(encoding="utf-8")
        )
        validate_fix_plan_set(plan)
        main_input_path = cli_out / "main-agent-input.json"
        main_input = json.loads(main_input_path.read_text(encoding="utf-8"))
        for name in ("target_environment_artifact", "target_context_artifact"):
            artifact = main_input[name]
            self.assertEqual(
                artifact["sha256"],
                hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(),
            )
        self.assertNotIn(
            api_key,
            main_input_path.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            api_key,
            (cli_out / "pi-agent-prompts" / "main-agent" / "attempt-1.txt").read_text(
                encoding="utf-8"
            ),
        )
        provenance_paths = sorted(
            (cli_out / "pi-agent-provenance").glob("task-*/attempt-1.json")
        )
        self.assertEqual(len(provenance_paths), 1)
        provenance = json.loads(provenance_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(provenance["thinking_level"], "off")


if __name__ == "__main__":
    unittest.main()
