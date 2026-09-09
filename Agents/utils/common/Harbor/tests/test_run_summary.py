"""Tests for the joint report published beside Harbor's summary.txt."""

import importlib.util
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
spec = importlib.util.spec_from_file_location("write_run_summary", SCRIPTS / "write_run_summary.py")
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)
from harbor_pi_runtime import PiProcessResult, PiRuntimeConfig


class RunSummaryTest(unittest.TestCase):
    def run_writer(self, root, *extra):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "write_run_summary.py"), str(root), *map(str, extra)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return (root / "summary.md").read_text()

    def test_combines_reports_and_refreshes_fixer_without_changing_raw_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = "status: complete\nRUN_ID: example\ntotal: 2\ncompleted: 1\nerrored: 1\nmean_reward: 0.5\n"
            (root / "summary.txt").write_text(raw)
            (root / "analyzer").mkdir()
            (root / "analyzer" / "benchmark-summary.md").write_text(
                "# Benchmark Summary\n\n## Analyzer Findings\n\nDependency failure.\n\n"
                "## Fixer Results\n\nNo Fixer report yet.\n"
            )
            first = self.run_writer(root)
            self.assertIn("Dependency failure.", first)
            self.assertNotIn("## Fixer\n", first)
            self.assertNotIn("No Fixer report yet.", first)
            (root / "fixer").mkdir()
            (root / "fixer" / "fix-report-latest.md").write_text(
                "# Fixer Report\n\n## Verification\n\nSmoke test passed; full rerun pending.\n"
            )
            final = self.run_writer(root)
            self.assertIn("| total | 2 |", final)
            self.assertIn("| mean_reward | 0.5 |", final)
            self.assertIn("Smoke test passed; full rerun pending.", final)
            self.assertNotIn("No Fixer report yet.", final)
            for name in ("Harbor", "Analyzer", "Fixer"):
                self.assertEqual(final.count(f"## {name}\n"), 1)
            self.assertEqual((root / "summary.txt").read_text(), raw)

    def test_missing_reports_are_explicit_and_do_not_invent_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_writer(Path(tmp))
            for name in ("Harbor", "Analyzer"):
                self.assertIn(f"{name} report unavailable", result)
            self.assertNotIn("## Fixer\n", result)
            self.assertNotIn("| total | 0 |", result)

    def test_custom_report_paths_and_first_occurrence_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("AGENT: <agent>|test\ndone: 2\ndone: 0\n")
            custom = root / "custom.md"
            custom.write_text("# Analysis\n\nCustom analysis.\n\n## Fixer Results\n\nNo repair.\n")
            result = self.run_writer(root, "--analyzer-summary", custom)
            self.assertIn("Custom analysis.", result)
            self.assertIn("| done | 2 |", result)
            self.assertIn("&lt;agent&gt;&#124;test", result)

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
            self.assertTrue((root / "summary.md").is_file())
            self.assertIn("| status | failed |", (root / "summary.md").read_text())


    def test_pi_narrative_uses_only_available_reports_and_preserves_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("total: 89\ncompleted: 56\n")
            (root / "analyzer").mkdir()
            (root / "analyzer" / "benchmark-summary.md").write_text(
                "# Benchmark Summary\n\nDependency failure.\n\n## Fixer Results\n\nNo report.\n"
            )
            result = PiProcessResult({"summary": "Dependency failures left trials incomplete."}, "", {}, None, "")
            with mock.patch.object(writer, "run_pi_json_process", return_value=result) as call:
                writer.write_run_summary(root, pi_config=PiRuntimeConfig(model="fixture"))
            rendered = (root / "summary.md").read_text()
            self.assertIn("Dependency failures left trials incomplete.", rendered)
            self.assertIn("| completed | 56 |", rendered)
            self.assertNotIn("## Fixer\n", rendered)
            payload = json.loads((root / "run-summary" / "summary-input.json").read_text())
            self.assertNotIn("fixer", payload)
            self.assertNotIn("No report.", payload["analyzer"])
            self.assertTrue(call.call_args.kwargs["no_tools"])
            self.assertTrue(call.call_args.kwargs["disable_context_files"])

    def test_pi_failure_or_invalid_output_keeps_deterministic_report(self):
        for result in (
            PiProcessResult(None, "", {}, "pi_dispatch_timeout", ""),
            PiProcessResult({"summary": []}, "", {}, None, ""),
        ):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "summary.txt").write_text("total: 2\n")
                with mock.patch.object(writer, "run_pi_json_process", return_value=result):
                    writer.write_run_summary(root, pi_config=PiRuntimeConfig(model="fixture"))
                rendered = (root / "summary.md").read_text()
                self.assertIn("Joint narrative unavailable", rendered)
                self.assertIn("| total | 2 |", rendered)


if __name__ == "__main__":
    unittest.main()
