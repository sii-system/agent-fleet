"""Integration contracts for the root benchmark summary."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import controller
import write_benchmark_summary as writer
from harbor_pi_runtime import PiProcessResult


class RunSummaryTest(unittest.TestCase):
    def test_cli_combines_available_reports_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = "status: failed\ntotal: 2\ncompleted: 1\nmean_reward: 0.5\n"
            (root / "summary.txt").write_text(raw)
            analyzer = root / "custom-analyzer"
            analyzer.mkdir()
            analysis = "# Benchmark Summary\n\nDependency failure.\n\n## Fixer Results\n\nNo report.\n"
            (analyzer / "benchmark-summary.md").write_text(analysis)
            args = [sys.executable, str(SCRIPTS / "write_benchmark_summary.py"),
                    "--run-dir", str(root), "--analyzer-output", str(analyzer), "--deterministic"]
            subprocess.run(args, check=True, capture_output=True, text=True)
            first = (root / "summary.md").read_text()
            self.assertIn("Dependency failure.", first)
            self.assertNotIn("## Fixer Results", first)
            (root / "fixer").mkdir()
            (root / "fixer" / "fix-report-latest.md").write_text(
                "# Fixer Report\n\n## Summary\n\nSmoke passed; full rerun pending.\n")
            subprocess.run(args, check=True, capture_output=True, text=True)
            final = (root / "summary.md").read_text()
            self.assertIn("| total | 2 |", final)
            self.assertIn("| mean_reward | 0.5 |", final)
            self.assertIn("Smoke passed; full rerun pending.", final)
            self.assertEqual(final.count("## Fixer Results"), 1)
            self.assertNotIn("No report.", final)
            self.assertEqual((root / "summary.txt").read_text(), raw)
            self.assertEqual((analyzer / "benchmark-summary.md").read_text(), analysis)

    def test_missing_monitor_does_not_invent_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer.publish_benchmark_summary(root, summarize=False)
            result = (root / "summary.md").read_text()
            self.assertIn("Harbor report unavailable", result)
            self.assertIn("Analyzer report unavailable", result)
            self.assertNotIn("0.00%", result)
            self.assertNotIn("No failed task required", result)
            self.assertNotIn("## Fixer Results", result)

    def test_registry_summary_producer_also_publishes_joint_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "write_harbor_registry_summary.py"),
                 "", str(root / "summary.txt"), "1", "example/dataset"],
                capture_output=True, text=True, check=False,
                env={**os.environ, "HARBOR_ANALYZER_OUTPUT_DIR": str(root / "analyzer")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("| status | failed |", (root / "summary.md").read_text())

    def test_single_pi_call_receives_all_reports_and_preserves_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("total: 89\ncompleted: 56\n")
            (root / "analyzer").mkdir()
            (root / "analyzer" / "benchmark-summary.md").write_text("Dependency failure.")
            (root / "fixer").mkdir()
            (root / "fixer" / "fix-report-latest.md").write_text("Smoke passed; full rerun pending.")
            result = PiProcessResult({"summary": "Dependency failures; smoke passed, full rerun pending.",
                                      "analysis_summary": [], "recommended_actions": []}, "", {}, None, "")
            with mock.patch.object(writer, "run_pi_json_process", return_value=result) as call:
                writer.publish_benchmark_summary(root)
            call.assert_called_once()
            payload = json.loads((root / "benchmark-summary" / "summary-input.json").read_text())
            self.assertIn("Smoke passed", payload["reports"]["fixer"])
            self.assertIn("Dependency failure", payload["reports"]["analyzer"])
            self.assertIn("total: 89", payload["reports"]["harbor"])
            self.assertTrue(call.call_args.kwargs["no_tools"])
            self.assertTrue(call.call_args.kwargs["disable_context_files"])
            self.assertIn("| completed | 56 |", (root / "summary.md").read_text())

    def test_pi_failure_keeps_deterministic_report_without_fixer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("total: 2\n")
            with mock.patch.object(writer, "run_pi_json_process", side_effect=OSError("unavailable")):
                writer.publish_benchmark_summary(root)
            self.assertIn("| total | 2 |", (root / "summary.md").read_text())
            payload = json.loads((root / "benchmark-summary" / "summary-input.json").read_text())
            self.assertNotIn("fixer", payload["reports"])

    def test_controller_summarizes_after_fixer_finishes_and_isolates_errors(self):
        root = Path("/fixture/run")
        state = {"status": "completed", "report_status": "available",
                 "config": {"analyzer_output": "/fixture/custom-analyzer"}}
        events = []
        def approve(*args):
            events.append("fixer_completed")
            return state
        def summarize(*args, **kwargs):
            self.assertEqual(events, ["fixer_completed"])
            self.assertEqual(kwargs["analyzer_output"], Path("/fixture/custom-analyzer"))
            raise OSError("summary unavailable")
        with mock.patch.object(controller, "approve_fixer", side_effect=approve), \
             mock.patch.object(controller, "fixer_status", return_value=state), \
             mock.patch.object(controller, "publish_benchmark_summary", side_effect=summarize) as call, \
             contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(controller._fixer_approve(root, "request"), 0)
        call.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")
        self.assertIn("summary unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
