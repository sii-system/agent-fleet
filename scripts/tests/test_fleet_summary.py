import json
import os
import subprocess
import sys
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


    def make_rl_queue(self, run, submission=None):
        queue = run / "runtime" / "claude-code" / "rl-queue"
        if submission:
            queue = queue / "jobs" / submission
        for state in ("pending", "active", "results"):
            (queue / state).mkdir(parents=True, exist_ok=True)
        return queue

    def write_rl_record(self, queue, state, request, **fields):
        payload = {"request_id": request, "ray_submission_id": queue.name, **fields}
        path = queue / state / f"{request}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_latest_rl_run_is_selected_over_older_eval(self):
        old = self.make_run("old-eval", 1)
        run = self.runs / "rl-run"
        queue = self.make_rl_queue(run, "submission-a")
        self.write_rl_record(queue, "results", "r1", ok=True, reward=1.0)
        result = self.summarize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# RL Rollout Summary", result.stdout)
        self.assertIn("Run ID: `rl-run`", result.stdout)
        self.assertIn("| Finished requests | 1 |", result.stdout)
        self.assertFalse((old / "summary.md").exists())
        self.assertFalse((run / "monitor").exists())

    def test_rl_snapshot_counts_requests_across_submissions_and_states(self):
        run = self.runs / "rl-run"
        first = self.make_rl_queue(run, "submission-a")
        second = self.make_rl_queue(run, "submission-b")
        self.write_rl_record(first, "results", "same-id", ok=True, reward=1.0)
        self.write_rl_record(first, "active", "same-id")
        self.write_rl_record(first, "results", "partial", ok=True, reward=0.5)
        self.write_rl_record(second, "results", "same-id", ok=True, reward=0.0)
        self.write_rl_record(second, "results", "error", ok=False, reward=None,
                             exception_info={"exception_type": "EnvironmentStartError"})
        self.write_rl_record(second, "results", "unknown", ok=True, reward=None)
        self.write_rl_record(first, "pending", "queued")
        self.write_rl_record(second, "active", "running")
        (first / "active" / "worker-1.current").write_text("running")
        (first / "results" / ".writing.json.tmp").write_text("{")
        result = self.summarize(str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        for row in ("| Queued requests | 1 |", "| Active requests | 1 |",
                    "| Finished requests | 5 |", "| Execution succeeded | 4 |",
                    "| Execution failed | 1 |", "| Mean reward | 0.5 (3 scored requests) |",
                    "| Missing reward | 2 |", "| EnvironmentStartError | 1 |"):
            self.assertIn(row, result.stdout)
        self.assertIn("Snapshot", result.stdout)
        self.assertIn("submission-a", result.stdout)
        self.assertIn("submission-b", result.stdout)
        self.assertNotIn("| Failure rate |", result.stdout)
        self.assertNotIn("## Analyzer", result.stdout)
        self.assertNotIn("## Fixer Results", result.stdout)
        payload = json.loads((run / "benchmark-summary" / "summary-input.json").read_text())
        self.assertEqual(payload["rollout"]["finished"], 5)
        self.assertEqual(payload["rollout"]["execution_failed"], 1)

    def test_empty_rl_listener_has_a_snapshot_without_claiming_completion(self):
        run = self.runs / "empty-rl"
        self.make_rl_queue(run)
        result = self.summarize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Finished requests | 0 |", result.stdout)
        self.assertIn("| Mean reward | unavailable |", result.stdout)
        self.assertIn("does not establish listener completion", result.stdout)

    def test_custom_rl_queue_directory_works_after_trial_pruning(self):
        queue = self.root / "custom queue"
        for state in ("pending", "active", "results"):
            (queue / state).mkdir(parents=True)
        request = self.write_rl_record(queue, "active", "r1")
        worker_root = self.root / "worker-trials"
        trial = worker_root / "old-trial"
        trial.mkdir(parents=True)
        result_file = trial / "result.json"
        result_file.write_text(json.dumps({"agent_result": {"rollout_details": ["large trace"]},
                                          "verifier_result": {"rewards": {"reward": 0.25}}}))
        record = queue / "results" / "r1.json"
        helper = SCRIPT.parent.parent / "Agents/utils/rl/rollout_worker_utils.py"
        subprocess.run([sys.executable, str(helper), "build-result", str(request),
                        str(result_file), str(trial / "console.log"), "0.25", "", "0",
                        str(record), "completed"], check=True, capture_output=True)
        (worker_root / "new-trial").mkdir()
        os.utime(trial, (1, 1))
        subprocess.run([sys.executable, str(helper), "prune-trials", str(worker_root),
                        "--keep", "1"], check=True, capture_output=True)
        self.assertFalse(trial.exists())
        before = record.read_bytes()
        result = self.summarize(str(queue))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.25 (1 scored requests)", result.stdout)
        self.assertEqual(record.read_bytes(), before)
        self.assertTrue((queue / "summary.md").is_file())
        payload = (queue / "benchmark-summary" / "summary-input.json").read_text()
        self.assertNotIn("large trace", payload)

    def test_rl_submission_named_token_remains_valid_summary_json(self):
        run = self.runs / "rl-run"
        queue = self.make_rl_queue(run, "submission-token")
        self.write_rl_record(queue, "results", "r1", ok=True, reward=1.0)
        result = self.summarize(str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("submission-token", result.stdout)

    def test_invalid_rl_result_fails_without_overwriting_summary(self):
        run = self.runs / "broken-rl"
        queue = self.make_rl_queue(run)
        (run / "summary.md").write_text("Previous report")
        for bad in ("{", "[]", '{"ok": true, "reward": "NaN"}'):
            with self.subTest(bad=bad):
                (queue / "results" / "bad.json").write_text(bad)
                result = self.summarize(str(run))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("bad.json", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual((run / "summary.md").read_text(), "Previous report")

    def test_rl_summary_reuses_analyzer_and_fixer_reports(self):
        run = self.runs / "rl-run"
        queue = self.make_rl_queue(run, "submission-a")
        self.write_rl_record(queue, "results", "r1", ok=False, reward=None)
        analyzer = run / "analyzer"
        analyzer.mkdir()
        report = analyzer / "analysis.json"
        report.write_text(json.dumps({"tasks": [{
            "task": {"task_index": "r1", "task_name": "task-a", "attempt_id": "r1"},
            "analysis_status": "analysis_complete", "final_class": "infra_fail",
            "root_cause_code": "container_runtime_failed",
            "root_cause_summary": "The task container did not start.",
        }]}))
        manifest = analyzer / "analyzer-artifacts-latest.json"
        manifest.write_text(json.dumps({"run_id": "rl-run", "publications": [
            {"artifacts": {"benchmark_report_path": str(report)}}]}))
        fixer = run / "fixer" / "fix-report-latest.md"
        fixer.parent.mkdir()
        fixer.write_text("# Fixer Report\n\n## Summary\n\nSmoke passed; full rerun pending.\n")
        sources = {p: p.read_bytes() for p in (report, manifest, fixer)}
        result = self.summarize(str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Infrastructure failure | 1 |", result.stdout)
        self.assertIn("The task container did not start.", result.stdout)
        self.assertIn("Smoke passed; full rerun pending.", result.stdout)
        self.assertEqual(result.stdout.count("## Fixer Results"), 1)
        self.assertEqual(sources, {p: p.read_bytes() for p in sources})
        manifest.write_text(json.dumps({"run_id": "different-run", "publications": []}))
        result = self.summarize(str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("The task container did not start.", result.stdout)

    def test_help_documents_optional_run(self):
        result = self.summarize("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("latest", result.stdout)


if __name__ == "__main__":
    unittest.main()
