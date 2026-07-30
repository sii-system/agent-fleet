"""Tests for the human-readable Harbor benchmark summary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import write_benchmark_summary as summary_writer
from harbor_pi_runtime import PiProcessResult


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _analysis(
    task_index: str,
    *,
    attempt_id: str,
    status: str,
    final_class: str,
    root_cause_code: str,
    root_cause_summary: str,
) -> dict[str, object]:
    return {
        "task": {
            "task_index": task_index,
            "task_name": f"task-{task_index}",
            "attempt_id": attempt_id,
        },
        "analysis_status": status,
        "final_class": final_class,
        "failure_stage": "agent_execution",
        "root_cause_code": root_cause_code,
        "root_cause_summary": root_cause_summary,
    }


def _pi_result(output_json: dict[str, object] | None, block_reason: str | None = None) -> PiProcessResult:
    return PiProcessResult(
        output_json=output_json,
        output_text="",
        provenance={},
        block_reason=block_reason,
        stderr_tail="",
    )


class BenchmarkSummaryTests(unittest.TestCase):
    def test_writes_llm_summary_from_deduplicated_analyzer_findings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            monitor_path = root_path / "monitor-latest.json"
            manifest_path = root_path / "analyzer-artifacts-latest.json"
            output_path = root_path / "benchmark-summary.md"
            first_report = root_path / "analyzer-runs" / "first.json"
            second_report = root_path / "analyzer-runs" / "second.json"

            _write_json(
                monitor_path,
                {
                    "benchmark_status": "completed",
                    "evidence": {"elapsed_since_run_start": 5025},
                    "task_summary": {
                        "complete_success": 7,
                        "complete_failed": 1,
                        "complete_unknown": 1,
                        "not_complete": 1,
                        "total_evaluated": 10,
                    },
                },
            )
            _write_json(
                first_report,
                {
                    "tasks": [
                        _analysis(
                            "1",
                            attempt_id="attempt-1",
                            status="analysis_complete",
                            final_class="env_fail",
                            root_cause_code="dependency_install_failed",
                            root_cause_summary="Dependency installation failed.",
                        ),
                        _analysis(
                            "2",
                            attempt_id="attempt-1",
                            status="analysis_failed",
                            final_class="unknown",
                            root_cause_code="insufficient_evidence",
                            root_cause_summary=(
                                "Analyzer subagent did not return a valid analysis: "
                                "child_task_json_validation_failed"
                            ),
                        ),
                    ]
                },
            )
            _write_json(
                second_report,
                {
                    "tasks": [
                        _analysis(
                            "1",
                            attempt_id="attempt-1",
                            status="analysis_complete",
                            final_class="model_fail",
                            root_cause_code="model_output_incorrect",
                            root_cause_summary="The model produced an incorrect answer.",
                        ),
                        _analysis(
                            "3",
                            attempt_id="attempt-1",
                            status="analysis_complete",
                            final_class="infra_fail",
                            root_cause_code="container_runtime_failed",
                            root_cause_summary="The task container did not start.",
                        ),
                    ]
                },
            )
            _write_json(
                manifest_path,
                {
                    "run_id": "benchmark-run",
                    "publications": [
                        {"artifacts": {"benchmark_report_path": str(first_report)}},
                        {"artifacts": {"benchmark_report_path": str(second_report)}},
                    ],
                },
            )
            model_output = {
                "summary": "Two task failures were analyzed; one analysis failed.",
                "analysis_summary": [
                    {"group_id": "G1", "summary": "The model returned an incorrect answer."},
                    {
                        "group_id": "G2",
                        "summary": "Analyzer retries ended because its response was invalid.",
                    },
                    {"group_id": "G3", "summary": "The task container failed to start."},
                ],
                "recommended_actions": [
                    {"group_ids": ["G1"], "action": "Review the model and verifier output."},
                    {"group_ids": ["G2"], "action": "Rerun Analyzer or inspect the task."},
                ],
            }

            with mock.patch.object(
                summary_writer,
                "run_pi_json_process",
                return_value=_pi_result(model_output),
            ) as run_pi:
                summary_writer.write_benchmark_summary(
                    monitor_path,
                    manifest_path,
                    output_path,
                )

            summary = output_path.read_text(encoding="utf-8")
            self.assertIn("Two task failures were analyzed; one analysis failed.", summary)
            self.assertIn("| Runtime | 1h 23m 45s |", summary)
            self.assertIn("| Success rate | 70.00% (7/10) |", summary)
            self.assertIn("| Failure rate | 30.00% (3/10) |", summary)
            self.assertIn("| Analysis complete | 2 |", summary)
            self.assertIn("| Environment failure | 0 |", summary)
            self.assertIn("| Infrastructure failure | 1 |", summary)
            self.assertIn("| Model failure | 1 |", summary)
            self.assertIn("| Unknown root cause | 0 |", summary)
            self.assertIn("| Analysis failed | 1 |", summary)
            self.assertIn(
                "**Analysis failed - `task-2 (attempt-1)`:** "
                "Analyzer retries ended because its response was invalid. "
                "The task's root cause remains undetermined.",
                summary,
            )
            self.assertNotIn("## Key Findings", summary)
            self.assertNotIn("## Analysis Notes", summary)

            kwargs = run_pi.call_args.kwargs
            self.assertTrue(kwargs["prompt_in_stdin"])
            self.assertTrue(kwargs["no_tools"])
            self.assertTrue(kwargs["no_builtin_tools"])
            self.assertTrue(kwargs["disable_extensions"])
            self.assertTrue(kwargs["disable_skills"])
            summary_input = json.loads(
                (root_path / "benchmark-summary" / "summary-input.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary_input["analysis_groups"][0]["final_class"], "model_fail")
            self.assertEqual(
                summary_input["analysis_groups"][1]["root_cause_summaries"],
                [
                    (
                        "Analyzer subagent did not return a valid analysis: "
                        "child_task_json_validation_failed"
                    )
                ],
            )
            self.assertEqual(
                json.loads(
                    (root_path / "benchmark-summary" / "summary-output.json").read_text(
                        encoding="utf-8"
                    )
                ),
                model_output,
            )

    def test_model_failure_keeps_deterministic_report(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            monitor_path = root_path / "monitor-latest.json"
            manifest_path = root_path / "analyzer-artifacts-latest.json"
            report_path = root_path / "analyzer-run.json"
            output_path = root_path / "benchmark-summary.md"
            _write_json(
                monitor_path,
                {
                    "benchmark_status": "completed",
                    "evidence": {"elapsed_since_run_start": 60},
                    "task_summary": {
                        "complete_success": 0,
                        "complete_failed": 1,
                        "complete_unknown": 0,
                        "not_complete": 0,
                        "total_evaluated": 1,
                    },
                },
            )
            _write_json(
                report_path,
                {
                    "tasks": [
                        _analysis(
                            "1",
                            attempt_id="attempt-1",
                            status="analysis_failed",
                            final_class="unknown",
                            root_cause_code="insufficient_evidence",
                            root_cause_summary="Analyzer timed out.",
                        )
                    ]
                },
            )
            _write_json(
                manifest_path,
                {
                    "run_id": "failed-summary-run",
                    "publications": [
                        {"artifacts": {"benchmark_report_path": str(report_path)}}
                    ],
                },
            )

            with mock.patch.object(
                summary_writer,
                "run_pi_json_process",
                return_value=_pi_result(None, "pi_dispatch_timeout"),
            ):
                summary_writer.write_benchmark_summary(
                    monitor_path,
                    manifest_path,
                    output_path,
                )

            summary = output_path.read_text(encoding="utf-8")
            self.assertIn("automated narrative summary is unavailable", summary)
            self.assertIn("Analyzer timed out.", summary)
            self.assertIn("The task's root cause remains undetermined.", summary)
            self.assertFalse(
                (root_path / "benchmark-summary" / "summary-output.json").exists()
            )

    def test_invalid_model_output_keeps_deterministic_report(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            monitor_path = root_path / "monitor-latest.json"
            output_path = root_path / "benchmark-summary.md"
            _write_json(
                monitor_path,
                {
                    "benchmark_status": "completed",
                    "evidence": {"elapsed_since_run_start": 0},
                    "task_summary": {
                        "complete_success": 1,
                        "complete_failed": 0,
                        "complete_unknown": 0,
                        "not_complete": 0,
                        "total_evaluated": 1,
                    },
                },
            )

            with mock.patch.object(
                summary_writer,
                "run_pi_json_process",
                return_value=_pi_result({"summary": "Incomplete output."}),
            ):
                summary_writer.write_benchmark_summary(
                    monitor_path,
                    root_path / "missing-manifest.json",
                    output_path,
                )

            self.assertIn(
                "automated narrative summary is unavailable",
                output_path.read_text(encoding="utf-8"),
            )

    def test_does_not_write_summary_while_monitor_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            monitor_path = root_path / "monitor-latest.json"
            output_path = root_path / "benchmark-summary.md"
            _write_json(monitor_path, {"benchmark_status": "running"})

            with self.assertRaisesRegex(ValueError, "benchmark_status=running"):
                summary_writer.write_benchmark_summary(
                    monitor_path,
                    root_path / "missing-manifest.json",
                    output_path,
                )

            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
