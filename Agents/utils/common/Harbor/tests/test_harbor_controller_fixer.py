"""Tests for the Controller-owned Harbor Fixer workflow."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import (
    FixerTestCase,
    make_fix_plan,
    write_analyzer_fixture,
    write_fixture_pi,
    write_json,
)
from harbor_controller.fixer import (
    approve_fixer,
    cancel_fixer,
    fixer_status,
    reset_fixer_control,
    start_fixer,
)
from harbor_fixer.executor import build_exec_input_from_plan


class HarborControllerFixerTest(FixerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.root / "run-1"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.analyzer_dir = write_analyzer_fixture(self.run_dir)
        write_json(
            self.run_dir / "monitor" / "user-notify-latest.json",
            {
                "run_id": "run-1",
                "benchmark_status": "completed",
                "controller_status": "completed",
            },
        )
        summary_path = self.run_dir / "analyzer" / "benchmark-summary.md"
        summary_path.write_text(
            "# Benchmark Summary\n\n## Fixer Results\n\n"
            "No Fixer report has been generated for this benchmark run.\n",
            encoding="utf-8",
        )
        (self.run_dir / "summary.txt").write_text(
            "DATASET_NAME: smith\nDATASET: /datasets/swesmith\nMODEL: source-model\n",
            encoding="utf-8",
        )
        fixture_pi = write_fixture_pi(self.root / "fixture-pi")
        self.env = mock.patch.dict(
            os.environ,
            {
                "HARBOR_FIXER_PI_BIN": str(fixture_pi),
                "HARBOR_FIXER_MODEL": "fixture-model",
                "HARBOR_FIXER_BASE_URL": "https://example.test/v1",
                "HARBOR_FIXER_API_KEY": "fixture",
                "HARBOR_FIXER_RERUN_TIMEOUT": "900",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.verify = mock.patch(
            "harbor_controller.fixer.run_verification_from_paths",
            return_value={"status": "fixed"},
        )
        self.write_report = mock.patch(
            "harbor_controller.fixer.write_fix_report",
            side_effect=lambda *args: args[-1].write_text(
                "# Harbor Fixer Report\n", encoding="utf-8"
            ),
        )
        self.update_summary = mock.patch(
            "harbor_controller.fixer.update_fixer_results"
        )
        self.verify_mock = self.verify.start()
        self.write_report_mock = self.write_report.start()
        self.update_summary_mock = self.update_summary.start()
        self.addCleanup(self.verify.stop)
        self.addCleanup(self.write_report.stop)
        self.addCleanup(self.update_summary.stop)

    def _start(self) -> dict:
        return start_fixer(self.run_dir, workspace_root=self.workspace)

    def test_start_exposes_reviewable_plan_and_approve_executes_it(self) -> None:
        state = self._start()

        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["paths"]["exec_result"], "")
        status = fixer_status(self.run_dir)
        self.assertEqual(status["available_actions"], ["approve", "cancel"])
        self.assertEqual(status["approval"]["plans"][0]["plan_id"], "fix-001")
        self.assertEqual(
            status["approval"]["automatic_follow_up"],
            [
                "execute_approved_plan",
                "run_smoke_verification",
                "write_fix_report",
                "update_benchmark_summary",
            ],
        )
        self.assertEqual(
            status["approval"]["plans"][0]["actions"][0]["executable"],
            "printf",
        )
        self.assertEqual(status["verification_status"], "not_available")
        self.assertEqual(status["report_status"], "not_available")
        self.assertEqual(
            list((self.run_dir / "fixer" / "active-agent-processes").glob("*.json")),
            [],
        )

        completed = approve_fixer(self.run_dir, state["approval_request_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["outcome"], "fixed")
        self.assertEqual(completed["verification_status"], "fixed")
        self.assertEqual(completed["report_status"], "available")
        self.assertEqual(completed["execution_counts"]["succeeded"], 1)
        self.assertEqual(
            completed["paths"]["exec_result"],
            str(self.run_dir / "fixer" / "exec-result-latest.json"),
        )
        self.assertTrue((self.run_dir / "fixer" / "exec-result-latest.json").is_file())
        self.verify_mock.assert_called_once()
        self.assertEqual(self.verify_mock.call_args.kwargs["dataset_name"], "smith")
        self.assertEqual(
            self.verify_mock.call_args.kwargs["dataset_path"],
            "/datasets/swesmith",
        )
        self.assertEqual(self.verify_mock.call_args.kwargs["model"], "source-model")
        self.assertEqual(self.verify_mock.call_args.kwargs["rerun_timeout"], 900)
        self.write_report_mock.assert_called_once()
        self.update_summary_mock.assert_called_once_with(
            self.run_dir / "analyzer" / "benchmark-summary.md",
            self.run_dir / "fixer" / "fix-report-latest.md",
        )

    def test_approve_exposes_automatic_verification_and_reporting_states(self) -> None:
        state = self._start()
        observed: list[str] = []

        def verify(*args: object, **kwargs: object) -> dict:
            status = fixer_status(self.run_dir)
            observed.append(str(status["status"]))
            self.assertEqual(status["verification_status"], "running")
            self.assertEqual(
                len(
                    list(
                        (self.run_dir / "fixer" / "active-agent-processes").glob(
                            "*-verification.json"
                        )
                    )
                ),
                1,
            )
            with self.assertRaisesRegex(ValueError, "after execution starts"):
                cancel_fixer(self.run_dir, state["fixer_workflow_id"])
            return {"status": "partially_fixed"}

        def report(*args: object) -> None:
            status = fixer_status(self.run_dir)
            observed.append(str(status["status"]))
            self.assertEqual(status["verification_status"], "partially_fixed")
            self.assertEqual(status["report_status"], "running")
            args[-1].write_text("# Harbor Fixer Report\n", encoding="utf-8")

        self.verify_mock.side_effect = verify
        self.write_report_mock.side_effect = report

        completed = approve_fixer(self.run_dir, state["approval_request_id"])

        self.assertEqual(observed, ["verifying", "reporting"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["outcome"], "partially_fixed")

    def test_verification_failure_stops_before_reporting(self) -> None:
        state = self._start()
        self.verify_mock.side_effect = ValueError("invalid verification result")

        with self.assertRaisesRegex(ValueError, "Fixer verification failed"):
            approve_fixer(self.run_dir, state["approval_request_id"])

        status = fixer_status(self.run_dir)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["outcome"], "verification_failed")
        self.assertEqual(status["verification_status"], "failed")
        self.assertEqual(status["report_status"], "not_available")
        self.write_report_mock.assert_not_called()
        self.update_summary_mock.assert_not_called()

    def test_reporting_failure_is_not_replaced_with_a_fallback(self) -> None:
        state = self._start()
        self.write_report_mock.side_effect = ValueError("invalid report input")

        with self.assertRaisesRegex(ValueError, "Fixer reporting failed"):
            approve_fixer(self.run_dir, state["approval_request_id"])

        status = fixer_status(self.run_dir)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["outcome"], "reporting_failed")
        self.assertEqual(status["verification_status"], "fixed")
        self.assertEqual(status["report_status"], "failed")
        self.update_summary_mock.assert_not_called()

    def test_status_survives_corrupt_state_and_missing_approval(self) -> None:
        state_path = self.run_dir / "fixer" / "fixer-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{invalid", encoding="utf-8")

        status = fixer_status(self.run_dir)

        self.assertEqual(status["status"], "error")
        self.assertEqual(status["available_actions"], [])
        self.assertIn("invalid JSON", status["error"]["message"])

        state_path.unlink()
        state = self._start()
        (self.run_dir / "fixer" / "fixer-approval-request.json").unlink()

        status = fixer_status(self.run_dir)

        self.assertEqual(status["status"], "awaiting_approval")
        self.assertEqual(status["approval_request_id"], state["approval_request_id"])
        self.assertIn("missing JSON", status["approval_error"])

    def test_start_requires_finished_benchmark_and_matching_analyzer(self) -> None:
        notification = self.run_dir / "monitor" / "user-notify-latest.json"
        payload = json.loads(notification.read_text(encoding="utf-8"))
        payload["benchmark_status"] = "running"
        write_json(notification, payload)
        with self.assertRaisesRegex(ValueError, "only after the benchmark"):
            self._start()

        payload["benchmark_status"] = "completed"
        write_json(notification, payload)
        manifest = self.analyzer_dir / "analyzer-artifacts-latest.json"
        analyzer_payload = json.loads(manifest.read_text(encoding="utf-8"))
        analyzer_payload["run_id"] = "other-run"
        write_json(manifest, analyzer_payload)
        with self.assertRaisesRegex(ValueError, "run_id does not match"):
            self._start()

    def test_start_requires_analyzer_handoffs_to_be_drained(self) -> None:
        write_json(
            self.run_dir / "monitor" / "analyzer-handover-latest.json",
            {"handover_id": "final-handover", "tasks": []},
        )

        with self.assertRaisesRegex(ValueError, "Analyzer still has pending"):
            self._start()

        write_json(
            self.analyzer_dir / ".analyzer_state.json",
            {"attempted_handover_keys": ["final-handover"]},
        )
        self.assertEqual(self._start()["status"], "awaiting_approval")

    def test_start_requires_published_benchmark_summary(self) -> None:
        (self.analyzer_dir / "benchmark-summary.md").unlink()

        with self.assertRaisesRegex(ValueError, "has not published"):
            self._start()

    def test_start_requires_fixer_results_section(self) -> None:
        (self.analyzer_dir / "benchmark-summary.md").write_text(
            "# Benchmark Summary\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "exactly one Fixer Results"):
            self._start()

    def test_custom_analyzer_output_owns_the_updated_summary(self) -> None:
        custom_root = self.root / "custom"
        custom_analyzer = write_analyzer_fixture(custom_root)
        custom_summary = custom_analyzer / "benchmark-summary.md"
        custom_summary.write_text(
            "# Benchmark Summary\n\n## Fixer Results\n\nNo Fixer report.\n",
            encoding="utf-8",
        )

        state = start_fixer(
            self.run_dir,
            workspace_root=self.workspace,
            analyzer_output=custom_analyzer,
        )
        approve_fixer(self.run_dir, state["approval_request_id"])

        self.update_summary_mock.assert_called_once_with(
            custom_summary,
            self.run_dir / "fixer" / "fix-report-latest.md",
        )

    def test_second_active_workflow_is_rejected(self) -> None:
        first = self._start()

        with self.assertRaisesRegex(ValueError, "another Fixer workflow is active"):
            self._start()

        self.assertEqual(
            fixer_status(self.run_dir)["fixer_workflow_id"],
            first["fixer_workflow_id"],
        )

    def test_live_planning_owner_rejects_start_and_reset(self) -> None:
        observed: dict[str, object] = {}

        def check_live_owner(*args: object, **kwargs: object) -> dict:
            with self.assertRaisesRegex(ValueError, "another Fixer workflow is active"):
                self._start()
            with self.assertRaisesRegex(ValueError, "cannot reset while Fixer"):
                reset_fixer_control(self.run_dir)
            observed["state_exists"] = (
                self.run_dir / "fixer" / "fixer-state.json"
            ).is_file()
            return {**make_fix_plan(), "plans": []}

        with mock.patch(
            "harbor_controller.fixer._run_planning", side_effect=check_live_owner
        ):
            state = self._start()

        self.assertTrue(observed["state_exists"])
        self.assertEqual(state["status"], "completed")

    def test_dead_active_owner_is_recovered_or_reset(self) -> None:
        state_path = self.run_dir / "fixer" / "fixer-state.json"
        write_json(
            state_path,
            {
                "fixer_workflow_id": "fixer-dead",
                "status": "executing",
                "owner": {"pid": 999999999, "start_ticks": 1},
            },
        )

        state = self._start()

        self.assertEqual(state["status"], "awaiting_approval")
        self.assertNotEqual(state["fixer_workflow_id"], "fixer-dead")

        state["status"] = "cancelling"
        state["owner"] = {
            "pid": 999999999,
            "start_ticks": 1,
        }
        write_json(state_path, state)
        reset = reset_fixer_control(self.run_dir)

        self.assertEqual(reset["status"], "reset")
        self.assertFalse(state_path.exists())

    def test_live_action_blocks_dead_owner_recovery_and_reset(self) -> None:
        state_path = self.run_dir / "fixer" / "fixer-state.json"
        write_json(
            state_path,
            {
                "fixer_workflow_id": "fixer-dead-owner",
                "status": "executing",
                "owner": {"pid": 999999999, "start_ticks": 1},
            },
        )
        start_ticks = int(
            Path(f"/proc/{os.getpid()}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()[19]
        )
        write_json(
            self.run_dir / "fixer" / "active-action.json",
            {"pid": os.getpid(), "start_ticks": start_ticks},
        )

        with self.assertRaisesRegex(ValueError, "another Fixer workflow is active"):
            self._start()
        with self.assertRaisesRegex(ValueError, "cannot reset while Fixer"):
            reset_fixer_control(self.run_dir)

        self.assertTrue(state_path.exists())

    def test_live_pi_process_blocks_dead_owner_recovery_and_reset(self) -> None:
        write_json(
            self.run_dir / "fixer" / "fixer-state.json",
            {
                "fixer_workflow_id": "fixer-dead-owner",
                "status": "planning",
                "owner": {"pid": 999999999, "start_ticks": 1},
            },
        )
        start_ticks = int(
            Path(f"/proc/{os.getpid()}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()[19]
        )
        write_json(
            self.run_dir / "fixer" / "active-agent-processes" / "agent.json",
            {"pid": os.getpid(), "start_ticks": start_ticks},
        )

        with self.assertRaisesRegex(ValueError, "another Fixer workflow is active"):
            self._start()
        with self.assertRaisesRegex(ValueError, "cannot reset while Fixer"):
            reset_fixer_control(self.run_dir)

    def test_cancel_rejects_pending_plan(self) -> None:
        state = self._start()

        cancelled = cancel_fixer(self.run_dir, state["fixer_workflow_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["outcome"], "user_rejected_plan")
        decision = json.loads(
            (self.run_dir / "fixer" / "fixer-user-decision.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(decision["decision"], "cancel")

    def test_cancel_during_planning_is_not_overwritten(self) -> None:
        def cancel_then_return(*args: object, **kwargs: object) -> dict:
            state = json.loads(
                (self.run_dir / "fixer" / "fixer-state.json").read_text(
                    encoding="utf-8"
                )
            )
            cancel_fixer(self.run_dir, state["fixer_workflow_id"])
            return make_fix_plan()

        with mock.patch(
            "harbor_controller.fixer._run_planning", side_effect=cancel_then_return
        ):
            state = self._start()

        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["outcome"], "cancelled_before_execution")

    def test_planning_exception_finishes_concurrent_cancel(self) -> None:
        def cancel_then_fail(*args: object, **kwargs: object) -> dict:
            state = json.loads(
                (self.run_dir / "fixer" / "fixer-state.json").read_text(
                    encoding="utf-8"
                )
            )
            cancel_fixer(self.run_dir, state["fixer_workflow_id"])
            raise RuntimeError("fixture failure")

        with mock.patch(
            "harbor_controller.fixer._run_planning", side_effect=cancel_then_fail
        ):
            state = self._start()

        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["outcome"], "cancelled_before_execution")

    def test_approve_blocks_if_reviewed_plan_changes(self) -> None:
        state = self._start()
        plan_path = self.run_dir / "fixer" / "fix-plan-latest.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["plans"][0]["actions"][0]["arguments"] = ["changed"]
        write_json(plan_path, plan)

        with self.assertRaisesRegex(ValueError, "changed after the approval"):
            approve_fixer(self.run_dir, state["approval_request_id"])

        status = fixer_status(self.run_dir)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["outcome"], "fix_plan_changed_after_review")

    def test_approve_executes_in_memory_plan_after_hash_check(self) -> None:
        state = self._start()
        plan_path = self.run_dir / "fixer" / "fix-plan-latest.json"
        reviewed = json.loads(plan_path.read_text(encoding="utf-8"))
        observed: dict[str, object] = {}

        def replace_disk_plan(
            fix_plan: dict,
            verified_plan_path: Path,
            workspace_root: Path,
        ) -> dict:
            replacement = json.loads(json.dumps(reviewed))
            replacement["plans"][0]["actions"][0]["arguments"] = ["replaced"]
            write_json(plan_path, replacement)
            observed["fix_plan"] = fix_plan
            return build_exec_input_from_plan(
                fix_plan,
                verified_plan_path,
                workspace_root,
            )

        with mock.patch(
            "harbor_controller.fixer.build_exec_input_from_plan",
            side_effect=replace_disk_plan,
        ):
            completed = approve_fixer(self.run_dir, state["approval_request_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(observed["fix_plan"], reviewed)

    def test_tampered_approval_plans_are_not_displayed_or_executed(self) -> None:
        state = self._start()
        approval_path = self.run_dir / "fixer" / "fixer-approval-request.json"
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["plans"][0]["actions"][0]["arguments"] = ["hidden-change"]
        write_json(approval_path, approval)

        status = fixer_status(self.run_dir)

        self.assertNotIn("approval", status)
        self.assertIn("approval plans do not match", status["approval_error"])
        with self.assertRaisesRegex(ValueError, "approval plans do not match"):
            approve_fixer(self.run_dir, state["approval_request_id"])

    def test_new_no_action_workflow_does_not_expose_previous_exec_result(self) -> None:
        first = self._start()
        approve_fixer(self.run_dir, first["approval_request_id"])
        self.assertTrue((self.run_dir / "fixer" / "exec-result-latest.json").exists())

        with mock.patch(
            "harbor_controller.fixer._run_planning",
            return_value={**make_fix_plan(), "plans": []},
        ):
            second = self._start()

        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["outcome"], "no_actions")
        self.assertEqual(second["paths"]["exec_result"], "")
        self.assertEqual(second["verification_status"], "not_required")
        self.assertEqual(second["report_status"], "not_required")

    def test_policy_denial_blocks_before_approval(self) -> None:
        denied = {
            "status": "denied",
            "decisions": [
                {
                    "plan_id": "fix-001",
                    "action_id": "action-001",
                    "decision": "deny",
                    "reason_code": "fixture_deny",
                    "reason": "Fixture denial.",
                }
            ],
        }
        with mock.patch(
            "harbor_controller.fixer.run_policy_preflight", return_value=denied
        ):
            state = self._start()

        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["outcome"], "policy_denied")
        self.assertEqual(state["verification_status"], "not_required")
        self.assertEqual(state["report_status"], "not_required")
        self.assertFalse(
            (self.run_dir / "fixer" / "fixer-approval-request.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
