"""Integration tests for the Fixer verification workflow."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import (
    FixerTestCase,
    make_exec_result,
    make_fix_plan,
    write_json,
)
from harbor_fixer.validation import ValidationError
from harbor_fixer.verification import workflow
from harbor_fixer.verifier import run_verification_from_paths


class HarborFixerVerificationWorkflowTest(FixerTestCase):
    def _fix_plan(self, agent: str = "claude-code") -> dict:
        plan = make_fix_plan()
        plan["source"].update(
            {
                "agent": agent,
                "monitor_path": "/source/monitor/monitor-latest.json",
            }
        )
        return plan

    def _inputs(self, plan: dict, execution: dict) -> tuple[Path, Path]:
        plan_path = self.root / "fix-plan.json"
        exec_path = self.root / "exec-result.json"
        write_json(plan_path, plan)
        write_json(exec_path, execution)
        return plan_path, exec_path

    def _run(
        self,
        plan: dict,
        plan_status: str = "success",
        *,
        policy_status: str = "allowed",
        agent: str | None = None,
        rerun_command: str | None = None,
        monitor_policy: str = "off",
    ) -> dict:
        execution = make_exec_result(
            plan_status, policy_status=policy_status, fix_plan=plan
        )
        if not plan["plans"]:
            execution["plans"] = []
        plan_path, exec_path = self._inputs(plan, execution)
        return run_verification_from_paths(
            plan_path,
            exec_path,
            self.root / "verification-run",
            self.root / "verification-output",
            agent=agent,
            rerun_command=rerun_command,
            monitor_policy=monitor_policy,
        )

    def test_successful_smoke_record_is_fixed(self) -> None:
        run_dir = self.root / "verification-run"
        queue_dir = run_dir / "queue" / "claude-code"
        queue_dir.mkdir(parents=True)
        (run_dir / "tasks.txt").write_text("task-1\n", encoding="utf-8")
        (queue_dir / "done.txt").write_text(
            "1\ttask-1\t1.0\t\t\n", encoding="utf-8"
        )
        (queue_dir / "failed.txt").write_text("", encoding="utf-8")

        result = self._run(self._fix_plan())

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(result["reason_codes"], [])
        self.assertEqual(result["agent"], "claude-code")
        self.assertEqual(result["rerun"]["agent"], "claude-code")
        self.assertEqual(
            result["source"]["analyzer_monitor_path"],
            "/source/monitor/monitor-latest.json",
        )
        self.assertEqual(
            result["task_results"][0]["verification_status"], "fixed"
        )

    def test_execution_failure_reason_reaches_result(self) -> None:
        denied = self._run(
            self._fix_plan(), "failed", policy_status="denied"
        )
        self.assertEqual(denied["reason_codes"], ["policy_denied"])
        self.assertEqual(
            denied["task_results"][0]["exec_failure_reason"], "policy_denied"
        )

        failed = self._run(self._fix_plan(), "failed")
        self.assertEqual(failed["reason_codes"], ["execution_failed"])

    def test_failed_rerun_does_not_wait_for_monitor(self) -> None:
        plan = self._fix_plan()

        with mock.patch.object(workflow, "wait_for_monitor") as wait_for_monitor:
            result = self._run(
                plan,
                rerun_command="false",
                monitor_policy="auto",
            )

        wait_for_monitor.assert_not_called()
        self.assertIn("rerun_failed", result["reason_codes"])

    def test_valid_input_defaults_optional_source_and_rerun_fields(self) -> None:
        plan = self._fix_plan()
        plan["source"].pop("monitor_path")
        execution = make_exec_result(fix_plan=plan)
        plan_path, exec_path = self._inputs(plan, execution)
        verification_input = workflow.build_verification_input(
            plan_path,
            exec_path,
            self.root / "verification-run",
            rerun_command=None,
            monitor_policy="off",
            output_dir=self.root / "verification-output",
        )
        verification_input.pop("rerun_command")

        result = workflow.run_verification(
            verification_input, self.root / "verification-output"
        )

        self.assertEqual(result["rerun"]["command"], "")
        self.assertEqual(result["source"]["analyzer_monitor_path"], "")
        self.assertEqual(verification_input["rerun_timeout"], 600)

    def test_clears_stale_result_before_input_validation(self) -> None:
        result_path = self.root / "verification-output" / "verification-result-latest.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text("stale\n", encoding="utf-8")
        plan = self._fix_plan()
        plan.pop("source")
        execution = make_exec_result(fix_plan=plan)
        plan_path, exec_path = self._inputs(plan, execution)

        with self.assertRaises(ValidationError):
            run_verification_from_paths(
                plan_path,
                exec_path,
                self.root / "verification-run",
                result_path.parent,
                monitor_policy="off",
            )

        self.assertFalse(result_path.exists())

    def test_duplicate_attempt_index_is_reported_as_unsampled(self) -> None:
        plan = self._fix_plan()
        first = plan["plans"][0]["task_list"][0]
        first["attempt_id"] = "attempt-1"
        plan["plans"][0]["task_list"].append(
            {**first, "attempt_id": "attempt-2"}
        )
        plan["plans"][0]["verification_hint"]["target_task_indexes"] = ["1", "1"]

        result = self._run(plan)

        plan_result = result["plan_results"][0]
        self.assertEqual(plan_result["sampled_task_indexes"], ["1"])
        self.assertEqual(plan_result["unsampled_task_indexes"], ["1"])
        self.assertEqual(plan_result["unsampled_task_count"], 1)

    def test_prestarted_benchmark_waits_without_rerun_command(self) -> None:
        completed = {"benchmark_status": "completed"}
        with (
            mock.patch.object(workflow, "process_is_alive", return_value=True),
            mock.patch.object(
                workflow,
                "wait_for_monitor",
                return_value=(completed, "monitor.json", False),
            ) as wait_for_monitor,
        ):
            self._run(self._fix_plan(), monitor_policy="auto")

        wait_for_monitor.assert_called_once()

    def test_empty_plan_reports_no_verifiable_tasks(self) -> None:
        plan = self._fix_plan()
        plan["plans"] = []

        result = self._run(plan)

        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["reason_codes"], ["no_verifiable_tasks"])

    def test_missing_plan_agent_requires_cli_selection(self) -> None:
        plan = self._fix_plan("")
        plan["source"]["monitor_path"] = ""
        execution = make_exec_result(fix_plan=plan)
        plan_path, exec_path = self._inputs(plan, execution)

        missing = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--verify-only",
                "--fix-plan",
                str(plan_path),
                "--exec-result",
                str(exec_path),
                "--verification-run-dir",
                str(self.root / "verification-run"),
                "--output-dir",
                str(self.root / "verification-output"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("pass --agent", missing.stderr)

        result = self._run(plan, agent="opencode")
        self.assertEqual(result["agent"], "opencode")
        self.assertEqual(result["rerun"]["agent"], "opencode")


if __name__ == "__main__":
    import unittest

    unittest.main()
