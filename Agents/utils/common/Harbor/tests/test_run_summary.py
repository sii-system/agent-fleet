"""Integration contracts for the root benchmark summary."""

import contextlib
import io
import json
import os
import re
import shutil
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

    def test_manual_refresh_uses_recorded_run_id_and_rejects_stale_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("RUN_ID: current-run\ntotal: 2\n")
            analyzer = root / "analyzer"
            analyzer.mkdir()
            for manifest in (None, {"run_id": "other-run", "publications": []},
                             {"run_id": "current-run", "publications": []}):
                with self.subTest(manifest=manifest):
                    manifest_path = analyzer / "analyzer-artifacts-latest.json"
                    if manifest is not None:
                        manifest_path.write_text(json.dumps(manifest))
                    (analyzer / "benchmark-summary.md").write_text("A diagnosis from the report.")
                    with mock.patch.dict(os.environ, {}, clear=True), \
                         mock.patch.object(writer, "_run_summary_model", return_value=(None, PiProcessResult(None, "", {}, "unavailable", ""))):
                        writer.publish_benchmark_summary(root)
                    payload = json.loads((root / "benchmark-summary" / "summary-input.json").read_text())
                    self.assertEqual(payload["run"]["run_id"], "current-run")
                    self.assertEqual("analyzer" in payload["reports"], manifest is not None and manifest["run_id"] == "current-run")
                    self.assertIn("Run ID: `current-run`", (root / "summary.md").read_text())

    def test_running_monitor_does_not_supply_final_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.txt").write_text("RUN_ID: run-1\ncompleted: 2\n")
            monitor_dir = root / "monitor"
            monitor_dir.mkdir()
            (monitor_dir / "monitor-latest.json").write_text(json.dumps({
                "benchmark_status": "running", "task_summary": {
                    "total_evaluated": 2, "complete_success": 1, "not_complete": 1,
                }, "task_handover": [],
            }))
            analyzer = root / "analyzer"
            analyzer.mkdir()
            (analyzer / "analyzer-artifacts-latest.json").write_text(json.dumps({"run_id": "run-1", "publications": []}))
            writer.publish_benchmark_summary(root, summarize=False)
            summary = (root / "summary.md").read_text()
            self.assertIn("| completed | 2 |", summary)
            self.assertNotIn("50.00%", summary)
            self.assertNotIn("None", summary)
            self.assertNotIn("No failed task required", summary)
            payload = json.loads((root / "benchmark-summary" / "summary-input.json").read_text())
            self.assertFalse(payload["monitor_available"])
            self.assertIsNone(payload["run"]["failed_tasks"])

    def test_missing_monitor_with_manifest_does_not_print_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "analyzer").mkdir()
            (root / "analyzer" / "analyzer-artifacts-latest.json").write_text(json.dumps({"run_id": "run-1", "publications": []}))
            writer.publish_benchmark_summary(root, summarize=False)
            summary = (root / "summary.md").read_text()
            self.assertIn("Analyzer results are unavailable", summary)
            self.assertNotIn("None", summary)

    def test_analyzer_disabled_shutdown_does_not_wait_for_monitor(self):
        source = (SCRIPTS.parent / "start.sh").read_text()
        function = re.search(r"harbor_finish_analyzer_lifecycle\(\) \{.*?\n\}", source, re.DOTALL)[0]
        for enabled, expected in (("0", "summary\n"), ("1", "wait\ndrain\nstop\nsummary\n")):
            result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + function + """
harbor_wait_for_monitor_completion() { echo wait; }
harbor_wait_for_analyzer_drain() { echo drain; }
harbor_stop_analyzer() { echo stop; }
harbor_write_benchmark_summary() { echo summary; }
harbor_finish_analyzer_lifecycle
"""], env={**os.environ, "ROLLOUT": "0", "HARBOR_ANALYZER_ENABLED": enabled}, capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(result.stdout, expected)

    def test_registry_exit_signal_follows_summary_publication_even_on_failure(self):
        source = (SCRIPTS.parent / "harboropik.sh").read_text()
        function = re.search(r"  record_harbor_benchmark_exit\(\) \{.*?\n  \}", source, re.DOTALL)[0]
        for summary_rc in ("0", "1"):
            with self.subTest(summary_rc=summary_rc), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + function + """
harbor_is_native_registry_main() { return 0; }
write_harbor_registry_summary() {
  if [[ -f "$HARBOR_BENCHMARK_EXIT_FILE" ]]; then echo early; fi
  return "$SUMMARY_RC"
}
harbor_stop_online_analysis() { return 0; }
harbor_is_fixer_verification_main() { return 1; }
trap record_harbor_benchmark_exit EXIT
exit 7
"""], env={**os.environ, "OUTPUT_PATH": tmp, "HARBOR_BENCHMARK_EXIT_FILE": str(Path(tmp) / "exit"), "SUMMARY_RC": summary_rc}, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 7)
                self.assertEqual((Path(tmp) / "exit").read_text(), "7\n")
                self.assertNotIn("early", result.stdout)

    def test_direct_cli_loads_saved_config_and_preserves_caller_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            entry_dir = repo / "Agents/utils/common/Harbor/scripts"
            shutil.copytree(SCRIPTS, entry_dir, ignore=shutil.ignore_patterns("__pycache__"))
            (repo / "scripts").mkdir()
            shutil.copyfile(SCRIPTS.parents[4] / "scripts/config_loader.sh", repo / "scripts/config_loader.sh")
            (repo / "config.env").write_text("MODEL=template-model\nBASE_URL=https://template.invalid/v1\n")
            (repo / "config.local.env").write_text("MODEL=saved-model\nBASE_URL=https://saved.invalid/v1\nAPI_KEY=fake-key\n")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pi = fake_bin / "pi"
            pi.write_text("#!" + sys.executable + "\n" + """import json, os, sys
if '--version' in sys.argv:
    print('fixture-pi 1.0')
else:
    from pathlib import Path
    provider = next(iter(json.loads((Path(os.environ['HOME']) / 'models.json').read_text())['providers'].values()))
    summary = provider['models'][0]['id'] + ' / ' + provider['baseUrl']
    output = {'summary': summary, 'analysis_summary': [], 'recommended_actions': []}
    print(json.dumps({'type': 'session', 'id': 'fixture-summary-session'}))
    print(json.dumps({'type': 'agent_start'}))
    print(json.dumps({'type': 'turn_start'}))
    print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': json.dumps(output)}], 'stopReason': 'stop'}}))
    print(json.dumps({'type': 'turn_end'}))
    print(json.dumps({'type': 'agent_end'}))
""")
            pi.chmod(0o755)
            run = root / "run"
            run.mkdir()
            (run / "summary.txt").write_text("RUN_ID: configured-run\n")
            env = {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "HOME": str(root)}
            for override, expected in (({}, "saved-model"), ({"MODEL": "caller-model"}, "caller-model")):
                result = subprocess.run([sys.executable, str(entry_dir / "write_benchmark_summary.py"), "--run-dir", str(run)], env={**env, **override}, capture_output=True, text=True, timeout=15, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = (run / "summary.md").read_text()
                self.assertIn(expected, summary, result.stderr)
                self.assertIn("https://saved.invalid/v1", summary)

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
