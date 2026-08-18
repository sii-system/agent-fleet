"""Contract and Harbor-state tests for Fixer verification."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, make_exec_result, make_fix_plan
from harbor_fixer.validation import (
    ValidationError,
    validate_exec_result,
    validate_verification_input,
    validate_verification_result,
)
from harbor_fixer.verification.run_state import (
    collect_task_results,
    generate_monitor_snapshot,
    read_monitor_snapshot,
)


def _empty_verification_result() -> dict:
    return {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "agent": "claude-code",
        "verification_mode": "smoke_test",
        "source": {},
        "execution": {"status": "success", "policy_status": "allowed"},
        "status": "inconclusive",
        "reason_codes": ["no_verifiable_tasks"],
        "rerun": {},
        "sampling": {"plan_task_count": 0},
        "new_run_summary": {},
        "plan_results": [],
        "task_results": [],
        "unexpected_run_task_results": [],
    }


def _successful_task_result() -> dict:
    payload = _empty_verification_result()
    payload.update(
        status="fixed",
        reason_codes=[],
        sampling={"plan_task_count": 1},
        task_results=[
            {
                "task": {
                    "task_index": "1",
                    "task_name": "task-one",
                    "attempt_id": None,
                },
                "verification_status": "fixed",
                "exec_status": "success",
                "exec_failure_reason": None,
                "new_run": {
                    "task_index": "1",
                    "task_name": "task-one",
                    "task_complete_status": "complete_success",
                },
            }
        ],
    )
    return payload


class HarborFixerVerificationContractsTest(FixerTestCase):
    def test_exec_result_requires_consistent_policy_status(self) -> None:
        validate_exec_result(make_exec_result())

        missing = make_exec_result()
        missing.pop("policy_status")
        with self.assertRaisesRegex(ValidationError, "policy_status"):
            validate_exec_result(missing)

        denied_success = make_exec_result(policy_status="denied")
        with self.assertRaisesRegex(ValidationError, "denied requires failed"):
            validate_exec_result(denied_success)

    def test_empty_verification_result_requires_reason(self) -> None:
        payload = _empty_verification_result()
        validate_verification_result(payload)

        invalid = copy.deepcopy(payload)
        invalid["reason_codes"] = []
        with self.assertRaisesRegex(ValidationError, "no_verifiable_tasks"):
            validate_verification_result(invalid)

    def test_verification_input_is_bound_to_executed_fix_plan(self) -> None:
        fix_plan = make_fix_plan()
        fix_plan["source"]["agent"] = "claude-code"
        payload = {
            "schema_version": 2,
            "kind": "harbor_fixer_verification_input",
            "fix_plan_path": "fix-plan-latest.json",
            "exec_result_path": "exec-result-latest.json",
            "verification_run_dir": "run",
            "output_dir": "output",
            "agent": "claude-code",
            "monitor_policy": "auto",
            "verification_mode": "smoke_test",
            "verification_task_limit_per_plan": 2,
            "fix_plan": fix_plan,
            "exec_result": make_exec_result(fix_plan=fix_plan),
        }
        validate_verification_input(payload)

        wrong_agent = copy.deepcopy(payload)
        wrong_agent["agent"] = "opencode"
        with self.assertRaisesRegex(ValidationError, "does not match fix_plan source"):
            validate_verification_input(wrong_agent)

        selected_agent = copy.deepcopy(payload)
        selected_agent["fix_plan"]["source"]["agent"] = ""
        selected_agent["exec_result"] = make_exec_result(
            fix_plan=selected_agent["fix_plan"]
        )
        validate_verification_input(selected_agent)

        selected_agent["agent"] = "unsupported"
        with self.assertRaisesRegex(ValidationError, "claude-code, opencode, oracle"):
            validate_verification_input(selected_agent)

        missing_digest = copy.deepcopy(payload)
        missing_digest["exec_result"]["source"].pop("fix_plan_sha256")
        with self.assertRaisesRegex(ValidationError, "fix_plan_sha256"):
            validate_verification_input(missing_digest)

        payload["fix_plan"]["plans"][0]["actions"][0]["purpose"] = "changed"
        with self.assertRaisesRegex(ValidationError, "does not match fix_plan"):
            validate_verification_input(payload)

    def test_task_verification_status_matches_execution_and_run(self) -> None:
        payload = _successful_task_result()
        validate_verification_result(payload)

        payload["task_results"][0]["new_run"]["task_complete_status"] = (
            "complete_failed"
        )
        with self.assertRaisesRegex(ValidationError, "must be not_fixed"):
            validate_verification_result(payload)

        task = payload["task_results"][0]
        task.update(
            verification_status="fixed",
            exec_status="failed",
            exec_failure_reason="execution_failed",
            new_run=None,
        )
        with self.assertRaisesRegex(ValidationError, "must be exec_failed"):
            validate_verification_result(payload)

    def test_verification_result_rejects_contradictory_summaries(self) -> None:
        denied = _successful_task_result()
        denied["execution"]["policy_status"] = "denied"
        with self.assertRaisesRegex(ValidationError, "requires failed"):
            validate_verification_result(denied)

        wrong_status = _successful_task_result()
        wrong_status["task_results"][0]["verification_status"] = "not_fixed"
        wrong_status["task_results"][0]["new_run"]["task_complete_status"] = (
            "complete_failed"
        )
        with self.assertRaisesRegex(ValidationError, "status must be not_fixed"):
            validate_verification_result(wrong_status)

        wrong_record = _successful_task_result()
        wrong_record["task_results"][0]["new_run"]["task_index"] = "2"
        with self.assertRaisesRegex(ValidationError, "identity does not match"):
            validate_verification_result(wrong_record)

    def test_collect_task_results_uses_harbor_classification(self) -> None:
        run_dir = self.root / "run"
        queue_dir = run_dir / "queue" / "claude-code"
        queue_dir.mkdir(parents=True)
        (run_dir / "task-manifest.tsv").write_text(
            "1\ttask-one\n", encoding="utf-8"
        )
        (queue_dir / "done.txt").write_text(
            "1\ttask-one\t1.0\t\t\n", encoding="utf-8"
        )
        (queue_dir / "failed.txt").write_text("", encoding="utf-8")
        source_state = run_dir / ".monitor_state.json"
        source_state.write_text('{"sentinel": true}\n', encoding="utf-8")

        records, summary = collect_task_results(run_dir, "claude-code")
        snapshot, _ = generate_monitor_snapshot(
            run_dir, self.root / "output", "claude-code"
        )

        self.assertEqual(records["1"]["task_complete_status"], "complete_success")
        self.assertEqual(summary["complete_success"], 1)
        self.assertEqual(snapshot["benchmark_status"], "completed")
        self.assertEqual(source_state.read_text(encoding="utf-8"), '{"sentinel": true}\n')

        other_queue = run_dir / "queue" / "opencode"
        other_queue.mkdir()
        (other_queue / "done.txt").write_text("", encoding="utf-8")
        explicit, _ = collect_task_results(run_dir, "claude-code")
        self.assertEqual(explicit["1"]["task_complete_status"], "complete_success")

    def test_native_harbor_results_use_smoke_indexes_and_monitor_metadata(self) -> None:
        run_dir = self.root / "native-run"
        runtime_dir = run_dir / "runtime" / "claude-code"
        job_dir = run_dir / "native-job"
        trial_dir = job_dir / "task-one__random"
        retry_dir = job_dir / "task-one__retry"
        prefixed_trial_dir = job_dir / "fix-git__random"
        runtime_dir.mkdir(parents=True)
        trial_dir.mkdir(parents=True)
        retry_dir.mkdir()
        prefixed_trial_dir.mkdir()
        (run_dir / "tasks.txt").write_text("task-one\nfix-git\n", encoding="utf-8")
        (runtime_dir / "harbor-job-dir").write_text(f"{job_dir}\n", encoding="utf-8")
        (runtime_dir / "harbor-benchmark.exit").write_text("0\n", encoding="utf-8")
        (job_dir / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 3,
                    "stats": {"n_running_trials": 0, "n_pending_trials": 0},
                    "finished_at": "2026-08-18T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": "task-one__random",
                    "task_name": "task-one",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": None,
                }
            ),
            encoding="utf-8",
        )
        (prefixed_trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": "fix-git__random",
                    "task_name": "terminal-bench/fix-git",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": None,
                }
            ),
            encoding="utf-8",
        )
        (retry_dir / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": "task-one__retry",
                    "task_name": "task-one",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": None,
                }
            ),
            encoding="utf-8",
        )

        records, summary = collect_task_results(run_dir, "claude-code")
        snapshot, _ = generate_monitor_snapshot(
            run_dir, self.root / "native-output", "claude-code"
        )

        self.assertEqual(set(records), {"1", "2"})
        self.assertEqual(records["1"]["task_name"], "task-one")
        self.assertEqual(records["1"]["task_complete_status"], "complete_success")
        self.assertEqual(records["1"]["result_path"], str(retry_dir / "result.json"))
        self.assertEqual(records["2"]["task_name"], "fix-git")
        self.assertEqual(records["2"]["task_complete_status"], "complete_success")
        self.assertEqual(summary["complete_success"], 2)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["benchmark_status"], "completed")
        self.assertEqual(snapshot["task_summary"]["total_evaluated"], 3)
        self.assertEqual(snapshot["task_summary"]["not_complete"], 0)
        self.assertFalse(snapshot["analyzer_handover"]["should_run_analyzer"])

        (prefixed_trial_dir / "result.json").unlink()
        partial, _ = generate_monitor_snapshot(
            run_dir, self.root / "partial-native-output", "claude-code"
        )

        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial["task_summary"]["total_evaluated"], 3)
        self.assertEqual(partial["task_summary"]["not_complete"], 1)
        self.assertTrue(partial["analyzer_handover"]["should_run_analyzer"])
        self.assertEqual(partial["analyzer_handover"]["tasks"][0]["task_name"], "fix-git")

        waiting_run = self.root / "waiting-native-run"
        waiting_runtime = waiting_run / "runtime" / "claude-code"
        waiting_runtime.mkdir(parents=True)
        (waiting_run / "tasks.txt").write_text("task-one\ntask-two\n", encoding="utf-8")
        (waiting_runtime / "harbor-job-dir").write_text("", encoding="utf-8")

        waiting, _ = generate_monitor_snapshot(
            waiting_run, self.root / "waiting-native-output", "claude-code"
        )

        self.assertIsNotNone(waiting)
        assert waiting is not None
        self.assertEqual(waiting["task_summary"]["total_evaluated"], 2)
        self.assertEqual(waiting["task_summary_action"]["total"], 2)
        self.assertTrue(waiting["analyzer_handover"]["should_run_analyzer"])
        self.assertEqual(len(waiting["analyzer_handover"]["tasks"]), 2)

    def test_read_monitor_snapshot_tolerates_partial_json(self) -> None:
        run_dir = self.root / "live-run"
        monitor_path = run_dir / "monitor" / "monitor-latest.json"
        monitor_path.parent.mkdir(parents=True)
        monitor_path.write_text('{"benchmark_status":', encoding="utf-8")

        self.assertEqual(read_monitor_snapshot(run_dir), (None, ""))


if __name__ == "__main__":
    unittest.main()
