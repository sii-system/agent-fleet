import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "run_fleet.sh"


class FleetSummaryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.env = {
            **os.environ,
            "REPO_DIR": str(self.root),
            "OUTPUT_ROOT": str(self.runs),
            "HARBOR_ANALYZER_MODEL": "",
            "HARBOR_ANALYZER_BASE_URL": "",
            "HARBOR_ANALYZER_API_KEY": "",
        }

    def make_run(self, name, started, status="completed"):
        run = self.runs / name
        monitor = run / "monitor" / "monitor-latest.json"
        monitor.parent.mkdir(parents=True)
        monitor.write_text(json.dumps({
            "benchmark_status": status,
            "evidence": {"run_start_ts": started},
            "task_summary": {
                "complete_success": 1,
                "complete_failed": 0,
                "complete_unknown": 0,
                "not_complete": 0,
                "total_evaluated": 1,
            },
        }), encoding="utf-8")
        return run

    def summarize(self, *args):
        return subprocess.run(
            [str(SCRIPT), "summary", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_defaults_to_latest_started_run_and_prints_report(self):
        latest = self.make_run("new-run", 200)
        older = self.make_run("old-run-touched-later", 100)
        (self.runs / "unrelated-directory").mkdir()
        result = self.summarize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run ID: `new-run`", result.stdout)
        self.assertIn("100.00% (1/1)", result.stdout)
        self.assertTrue((latest / "summary.md").is_file())
        self.assertFalse((older / "analyzer").exists())

    def test_explicit_id_selects_older_stopped_run(self):
        self.make_run("stopped-run", 100, "stopped")
        self.make_run("new-run", 200)
        result = self.summarize("stopped-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run ID: `stopped-run`", result.stdout)

    def test_preserves_existing_analyzer_report(self):
        run = self.make_run("completed-run", 100)
        report = run / "analyzer" / "benchmark-summary.md"
        report.parent.mkdir()
        report.write_text("Existing analysis\n", encoding="utf-8")
        result = self.summarize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report.read_text(encoding="utf-8"), "Existing analysis\n")

    def test_accepts_directory_with_spaces(self):
        run = self.make_run("custom run", 100)
        result = self.summarize(str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run ID: `custom run`", result.stdout)

    def test_latest_running_run_does_not_fall_back_to_older_result(self):
        older = self.make_run("old-run", 100)
        self.make_run("active-run", 200, "running")
        result = self.summarize()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active-run", result.stderr)
        self.assertIn("running", result.stderr)
        self.assertNotIn("monitor-latest.json", result.stderr)
        self.assertFalse((older / "analyzer").exists())

    def test_missing_explicit_run_does_not_fall_back(self):
        self.make_run("new-run", 200)
        result = self.summarize("missing-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing-run", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_no_runs_reports_actionable_error(self):
        result = self.summarize()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No Harbor runs found", result.stderr)

    def test_configured_output_root_is_used(self):
        self.make_run("configured-run", 200)
        self.env.pop("OUTPUT_ROOT")
        (self.root / "config.local.env").write_text(
            f"OUTPUT_ROOT='{self.runs}'\n", encoding="utf-8",
        )
        result = self.summarize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run ID: `configured-run`", result.stdout)

    def test_help_documents_optional_run(self):
        result = self.summarize("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("latest", result.stdout)


if __name__ == "__main__":
    unittest.main()
