"""Tests for Harbor analyzer contract and validation helpers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.scripts.harbor_analyzer.contract import (
    validate_handover,
)
from Agents.utils.common.Harbor.scripts.harbor_analyzer.validation import (
    validate_final_json,
    validate_task_analysis,
)

HANDOVER_ID = "sha256-" + ("a" * 64)
RUN_ID = "run-1"


def analyzer_task() -> dict[str, object]:
    return {
        "task_index": "1",
        "task_name": "demo-task",
        "attempt_id": None,
        "task_complete_status": "complete_failed",
        "task_result_signals": ["missing_required_field"],
        "result_path": "jobs/demo/job.log",
    }


def handover(run_dir: Path, queue_dir: Path) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "task_analysis_handover",
        "audience": "analyzer_subagent",
        "handover_id": HANDOVER_ID,
        "run_id": RUN_ID,
        "should_run_analyzer": True,
        "analyze_statuses": ["complete_failed", "complete_unknown", "not_complete"],
        "skip_statuses": ["complete_success"],
        "signal_definitions": {
            "missing_required_field": "Task config is missing a required field.",
        },
        "paths": {
            "run_dir": str(run_dir),
            "queue_dir": str(queue_dir),
        },
        "tasks": [analyzer_task()],
    }


def env_task_analysis(evidence_path: Path) -> dict[str, object]:
    task = analyzer_task()
    return {
        "schema_version": 2,
        "kind": "harbor_task_root_cause_analysis",
        "handover_id": HANDOVER_ID,
        "run_id": RUN_ID,
        "task": {
            "task_index": task["task_index"],
            "task_name": task["task_name"],
            "attempt_id": task["attempt_id"],
        },
        "analysis_status": "analysis_complete",
        "final_class": "env_fail",
        "failure_stage": "task_config",
        "root_cause_code": "task_config_missing_required_field",
        "root_cause_summary": "Task config is missing a required field.",
        "scope": "task",
        "confidence": 0.9,
        "observations": [
            {
                "path": str(evidence_path),
                "line_start": 2,
                "line_end": 2,
                "fact": "The required field is missing.",
            }
        ],
        "reasoning_summary": "The verifier failed before model output because task config was incomplete.",
        "alternatives_considered": [],
        "recommended_events": ["notify_user"],
        "fix_references": [
            {
                "path": str(evidence_path),
                "line_start": 2,
                "line_end": 2,
                "fact": "The required field is missing.",
                "reason": "This line directly identifies the task config issue.",
            }
        ],
    }


def final_json(task_report: dict[str, object]) -> dict[str, object]:
    task = task_report["task"]
    return {
        "schema_version": 2,
        "kind": "harbor_analyzer_final",
        "handover_id": HANDOVER_ID,
        "run_id": RUN_ID,
        "benchmark_report": {
            "schema_version": 2,
            "kind": "harbor_benchmark_root_cause_report",
            "handover_id": HANDOVER_ID,
            "run_id": RUN_ID,
            "tasks": [task_report],
        },
        "env_infra_tasks": {
            "schema_version": 2,
            "kind": "harbor_env_infra_task_list",
            "handover_id": HANDOVER_ID,
            "tasks": [
                {
                    "task": task,
                    "final_class": task_report["final_class"],
                    "failure_stage": task_report["failure_stage"],
                    "scope": task_report["scope"],
                    "confidence": task_report["confidence"],
                    "root_cause_code": task_report["root_cause_code"],
                    "root_cause_summary": task_report["root_cause_summary"],
                }
            ],
        },
        "fix_line_index": [
            {
                "schema_version": 2,
                "kind": "harbor_fix_line_reference",
                "task": task,
                "root_cause_code": task_report["root_cause_code"],
                "path": task_report["fix_references"][0]["path"],
                "line_start": task_report["fix_references"][0]["line_start"],
                "line_end": task_report["fix_references"][0]["line_end"],
                "fact": task_report["fix_references"][0]["fact"],
                "reason": task_report["fix_references"][0]["reason"],
            }
        ],
    }


class HarborAnalyzerValidationTest(unittest.TestCase):
    def test_validate_handover_accepts_monitor_shaped_payload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            payload = handover(run_dir, queue_dir)

            self.assertEqual(validate_handover(payload, run_dir=run_dir, queue_dir=queue_dir), payload["tasks"])

    def test_validate_handover_rejects_duplicate_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            payload = handover(run_dir, queue_dir)
            duplicate = dict(analyzer_task())
            first = dict(analyzer_task())
            first["attempt_id"] = 0
            duplicate["attempt_id"] = "0"
            payload["tasks"] = [first, duplicate]

            with self.assertRaisesRegex(ValueError, "duplicate_identity"):
                validate_handover(payload, run_dir=run_dir, queue_dir=queue_dir)

    def test_validate_task_analysis_accepts_grounded_env_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            evidence_path = Path(root) / "job.log"
            evidence_path.write_text("start\nmissing required field\nend\n", encoding="utf-8")
            audit_path = Path(root) / "tool-access.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "tool": "read",
                        "allowed": True,
                        "resolved_path": str(evidence_path.resolve()),
                        "line_start": 1,
                        "line_end": 3,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                validate_task_analysis(
                    env_task_analysis(evidence_path),
                    handover_id=HANDOVER_ID,
                    expected_task=analyzer_task(),
                    tool_access_audit_path=audit_path,
                ),
                [],
            )

    def test_validate_task_analysis_rejects_partially_grounded_fix_reference(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            evidence_path = Path(root) / "job.log"
            evidence_path.write_text("start\nmissing required field\nend\n", encoding="utf-8")
            audit_path = Path(root) / "tool-access.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "tool": "read",
                        "allowed": True,
                        "resolved_path": str(evidence_path.resolve()),
                        "line_start": 2,
                        "line_end": 2,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = env_task_analysis(evidence_path)
            report["fix_references"][0]["line_start"] = 1
            report["fix_references"][0]["line_end"] = 3

            self.assertIn(
                "task_0_fix_reference_0_not_grounded_in_tool_audit",
                validate_task_analysis(
                    report,
                    handover_id=HANDOVER_ID,
                    expected_task=analyzer_task(),
                    tool_access_audit_path=audit_path,
                ),
            )

    def test_validate_handover_rejects_redefined_status_policy(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            payload = handover(run_dir, queue_dir)
            payload["analyze_statuses"] = ["complete_success"]
            payload["skip_statuses"] = []
            payload["tasks"][0]["task_complete_status"] = "complete_success"

            with self.assertRaisesRegex(ValueError, "status_policy"):
                validate_handover(payload, run_dir=run_dir, queue_dir=queue_dir)

    def test_validate_handover_rejects_result_path_outside_evidence_roots(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            payload = handover(run_dir, queue_dir)
            payload["tasks"][0]["result_path"] = "/etc/passwd"

            with self.assertRaisesRegex(ValueError, "result_path_outside_evidence_roots"):
                validate_handover(payload, run_dir=run_dir, queue_dir=queue_dir)

    def test_validate_final_json_accepts_complete_env_infra_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            report = env_task_analysis(Path(root) / "job.log")

            self.assertEqual(
                validate_final_json(
                    final_json(report),
                    handover_id=HANDOVER_ID,
                    handover_tasks=[analyzer_task()],
                ),
                [],
            )

    def test_validate_final_json_rejects_env_infra_report_omitted_from_task_list(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload = final_json(env_task_analysis(Path(root) / "job.log"))
            payload = copy.deepcopy(payload)
            payload["env_infra_tasks"]["tasks"] = []
            payload["fix_line_index"] = []

            self.assertIn(
                "benchmark_report_env_infra_not_in_env_infra_tasks=[('1', 'demo-task', '')]",
                validate_final_json(
                    payload,
                    handover_id=HANDOVER_ID,
                    handover_tasks=[analyzer_task()],
                ),
            )


if __name__ == "__main__":
    unittest.main()
