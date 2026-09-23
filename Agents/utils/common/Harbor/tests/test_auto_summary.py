"""Automatic summary lifecycle and opt-in report contracts."""

import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_summary
import summarize_run
import write_benchmark_summary as writer
from harbor_pi_runtime import PiProcessResult


class AutoSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "summary.txt").write_text("RUN_ID: smoke\nstatus: complete\ntotal: 1\ncompleted: 1\n")
        self.model = mock.patch.object(writer, "run_pi_json_process", return_value=PiProcessResult(
            {"summary": "Completed one trial.", "analysis_summary": [], "recommended_actions": []},
            "", {}, None, "",
        )).start()
        self.addCleanup(mock.patch.stopall)

    def test_default_calls_model_once_without_starting_analyzer_or_fixer(self):
        self.assertTrue(auto_summary.auto_summary(self.root, defer_analyzer=True))
        self.assertFalse(auto_summary.auto_summary(self.root))
        self.model.assert_called_once()
        report = (self.root / "summary.md").read_text()
        self.assertIn("Completed one trial.", report)
        self.assertIn("| completed | 1 |", report)
        self.assertNotIn("Analyzer", report)
        self.assertNotIn("Fixer", report)
        payload = json.loads((self.root / "benchmark-summary/summary-input.json").read_text())
        self.assertEqual(set(payload["reports"]), {"harbor"})
        self.assertFalse((self.root / "analyzer").exists())
        self.assertFalse((self.root / "fixer").exists())

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires fork")
    def test_concurrent_completion_hooks_publish_only_once(self):
        counter = self.root / "model-calls.txt"
        result = self.model.return_value

        def model(**kwargs):
            with counter.open("a") as stream:
                stream.write("called\n")
            time.sleep(0.1)
            return result

        self.model.side_effect = model
        context = multiprocessing.get_context("fork")
        workers = [context.Process(target=auto_summary.auto_summary, args=(self.root,)) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(counter.read_text(), "called\n")
        self.assertTrue((self.root / "benchmark-summary/summary-output.json").is_file())

    def test_new_terminal_report_is_not_hidden_by_previous_completion_marker(self):
        self.assertTrue(auto_summary.auto_summary(self.root))
        (self.root / "summary.txt").write_text("RUN_ID: smoke\ncompleted: 2\n")
        self.assertTrue(auto_summary.auto_summary(self.root))
        self.assertEqual(self.model.call_count, 2)
        self.assertIn("| completed | 2 |", (self.root / "summary.md").read_text())

    def test_disable_leaves_manual_summary_available_without_monitor(self):
        os.environ["HARBOR_SUMMARY_ENABLED"] = "0"
        self.assertFalse(auto_summary.auto_summary(self.root))
        self.assertFalse((self.root / "summary.md").exists())
        self.model.assert_not_called()
        self.assertIn("Completed one trial.", summarize_run.summarize_run(self.root))
        self.model.assert_called_once()

    def test_rollout_and_fixer_verification_do_not_summarize_automatically(self):
        for name in ("ROLLOUT", "HARBOR_FIXER_VERIFICATION_RERUN"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "1"}):
                self.assertFalse(auto_summary.auto_summary(self.root))
        self.model.assert_not_called()

    def test_no_completion_report_does_not_publish(self):
        (self.root / "summary.txt").unlink()
        self.assertFalse(auto_summary.auto_summary(self.root))
        self.model.assert_not_called()

    def test_disabled_analyzer_ignores_even_malformed_existing_artifacts(self):
        analyzer = self.root / "analyzer"
        analyzer.mkdir()
        (analyzer / "analyzer-artifacts-latest.json").write_text("not json")
        (analyzer / "benchmark-summary.md").write_text("stale diagnosis")
        auto_summary.auto_summary(self.root)
        payload = json.loads((self.root / "benchmark-summary/summary-input.json").read_text())
        self.assertFalse(payload["analyzer_enabled"])
        self.assertEqual(payload["analysis_groups"], [])
        self.assertNotIn("analyzer", payload["reports"])

    def test_opted_in_analyzer_waits_for_lifecycle_and_includes_requested_fixer(self):
        os.environ["HARBOR_ANALYZER_ENABLED"] = "1"
        analyzer = self.root / "analyzer"
        analyzer.mkdir()
        (analyzer / "analyzer-artifacts-latest.json").write_text(json.dumps({"run_id": "smoke", "publications": []}))
        (analyzer / "benchmark-summary.md").write_text("Requested analysis.")
        fixer = self.root / "fixer"
        fixer.mkdir()
        (fixer / "fix-report-latest.md").write_text("# Fixer Report\n\n## Summary\n\nRequested repair completed.\n")
        self.assertFalse(auto_summary.auto_summary(self.root, defer_analyzer=True))
        self.model.assert_not_called()
        self.assertTrue(auto_summary.auto_summary(self.root))
        report = (self.root / "summary.md").read_text()
        self.assertIn("## Analyzer", report)
        self.assertIn("Requested repair completed.", report)
        payload = json.loads((self.root / "benchmark-summary/summary-input.json").read_text())
        self.assertIn("analyzer", payload["reports"])
        self.assertIn("fixer", payload["reports"])

    def test_model_failure_preserves_report_and_does_not_retry_at_other_hook(self):
        self.model.side_effect = OSError("model offline")
        self.assertTrue(auto_summary.auto_summary(self.root))
        self.assertFalse(auto_summary.auto_summary(self.root))
        self.model.assert_called_once()
        self.assertIn("| completed | 1 |", (self.root / "summary.md").read_text())

    def test_completed_report_is_usable_before_optional_monitor_observes_exit(self):
        monitor = self.root / "monitor"
        monitor.mkdir()
        (monitor / "monitor-latest.json").write_text(json.dumps({"benchmark_status": "running"}))
        self.assertTrue(auto_summary.auto_summary(self.root))
        report = (self.root / "summary.md").read_text()
        self.assertIn("| completed | 1 |", report)
        self.assertNotIn("0.00%", report)


if __name__ == "__main__":
    unittest.main()
