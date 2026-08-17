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
    start_fixer,
)


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
        fixture_pi = write_fixture_pi(self.root / "fixture-pi")
        self.env = mock.patch.dict(
            os.environ,
            {
                "HARBOR_FIXER_PI_BIN": str(fixture_pi),
                "HARBOR_FIXER_MODEL": "fixture-model",
                "HARBOR_FIXER_BASE_URL": "https://example.test/v1",
                "HARBOR_FIXER_API_KEY": "fixture",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _start(self) -> dict:
        return start_fixer(self.run_dir, workspace_root=self.workspace)

    def test_start_exposes_reviewable_plan_and_approve_executes_it(self) -> None:
        state = self._start()

        self.assertEqual(state["status"], "awaiting_approval")
        status = fixer_status(self.run_dir)
        self.assertEqual(status["available_actions"], ["approve", "cancel"])
        self.assertEqual(status["approval"]["plans"][0]["plan_id"], "fix-001")
        self.assertEqual(
            status["approval"]["plans"][0]["actions"][0]["executable"],
            "printf",
        )
        self.assertEqual(status["verification_status"], "not_available")
        self.assertEqual(status["report_status"], "not_available")

        completed = approve_fixer(self.run_dir, state["approval_request_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["execution_counts"]["succeeded"], 1)
        self.assertTrue((self.run_dir / "fixer" / "exec-result-latest.json").is_file())

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

    def test_second_active_workflow_is_rejected(self) -> None:
        first = self._start()

        with self.assertRaisesRegex(ValueError, "another Fixer workflow is active"):
            self._start()

        self.assertEqual(
            fixer_status(self.run_dir)["fixer_workflow_id"],
            first["fixer_workflow_id"],
        )

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
        self.assertFalse(
            (self.run_dir / "fixer" / "fixer-approval-request.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
