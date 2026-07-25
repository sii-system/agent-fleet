"""Tests for Harbor console online analysis summaries."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "online_rule_analyzer.py"
SPEC = importlib.util.spec_from_file_location("online_rule_analyzer", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class OnlineRuleAnalyzerTest(unittest.TestCase):
    def test_monitor_summary_only_includes_structured_environment_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            console = run_dir / "1-fix-git.console.log"
            console.write_text(
                "\n".join(
                    [
                        (
                            '[ONLINE_ENV] {"schema":1,"task_id":null,"phase":"preflight",'
                            '"component":"docker","event":"daemon_unavailable","severity":"critical",'
                            '"fatal":true,"scope":"task","message":"docker info failed"}'
                        ),
                        "AgentTimeoutError",
                        "NonZeroAgentExitCodeError",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            analyzer = MODULE.Analyzer(
                Namespace(run_dir=run_dir, output_dir=None, follow=False, poll_interval=1.0)
            )

            analyzer.run()

            summary = json.loads(analyzer.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["event_count"], 3)
            self.assertEqual(summary["task_blocking_event_count"], 1)
            self.assertEqual(
                summary["monitor_environment_events_by_type"],
                {"docker.daemon_unavailable": 1},
            )
            self.assertEqual(summary["events_by_type"]["agent-timeout"], 1)
            self.assertEqual(summary["events_by_type"]["agent-process-exit-abnormal"], 1)

    def test_replay_flushes_final_line_without_newline(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            console = run_dir / "1-fix-git.console.log"
            console.write_text(
                '[ONLINE_ENV] {"schema":1,"task_id":null,"phase":"preflight",'
                '"component":"docker","event":"daemon_unavailable","severity":"critical",'
                '"fatal":true,"scope":"task","message":"docker info failed"}',
                encoding="utf-8",
            )
            analyzer = MODULE.Analyzer(
                Namespace(run_dir=run_dir, output_dir=None, follow=False, poll_interval=1.0)
            )

            analyzer.run()

            summary = json.loads(analyzer.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(
                summary["monitor_environment_events_by_type"],
                {"docker.daemon_unavailable": 1},
            )

    def test_follow_flushes_complete_partial_after_idle_window(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            console = run_dir / "1-fix-git.console.log"
            console.write_text(
                '[ONLINE_ENV] {"schema":1,"task_id":null,"phase":"preflight",'
                '"component":"docker","event":"daemon_unavailable","severity":"critical",'
                '"fatal":true,"scope":"task","message":"docker info failed"}',
                encoding="utf-8",
            )
            analyzer = MODULE.Analyzer(
                Namespace(run_dir=run_dir, output_dir=None, follow=True, poll_interval=1.0)
            )
            analyzer.output_dir.mkdir()
            analyzer.events_path.write_text("", encoding="utf-8")

            with mock.patch.object(MODULE.time, "monotonic", side_effect=[0.0, 1.0, 2.0]):
                analyzer.scan_once()
                analyzer.flush_partials()
                self.assertEqual(analyzer.events, [])
                analyzer.flush_partials()

            self.assertEqual(len(analyzer.events), 1)
            self.assertEqual(analyzer.events[0].event, "daemon_unavailable")

    def test_follow_keeps_incomplete_structured_partial_after_idle_window(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            console = run_dir / "1-fix-git.console.log"
            console.write_text('[ONLINE_ENV] {"schema":1', encoding="utf-8")
            analyzer = MODULE.Analyzer(
                Namespace(run_dir=run_dir, output_dir=None, follow=True, poll_interval=1.0)
            )
            analyzer.output_dir.mkdir()
            analyzer.events_path.write_text("", encoding="utf-8")

            with mock.patch.object(MODULE.time, "monotonic", side_effect=[0.0, 2.0]):
                analyzer.scan_once()
                analyzer.flush_partials()

            self.assertEqual(analyzer.events, [])
            self.assertEqual(analyzer.partials[console.resolve()], '[ONLINE_ENV] {"schema":1')

    def test_seta_profile_reports_job_log_environment_signals(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            console = run_dir / "1-seta-task.console.log"
            console.write_text("AgentTimeoutError\n", encoding="utf-8")
            job_log = (
                run_dir
                / "jobs"
                / "claude-code"
                / "worker-1"
                / "1-seta-task"
                / "2026-06-04__00-00-00"
                / "2026-06-04__00-00-01"
                / "job.log"
            )
            job_log.parent.mkdir(parents=True)
            job_log.write_text(
                "E: Could not open lock file /var/lib/apt/lists/lock - open (13: Permission denied)\n",
                encoding="utf-8",
            )
            analyzer = MODULE.Analyzer(
                Namespace(run_dir=run_dir, output_dir=None, follow=False, poll_interval=1.0, profile="seta")
            )

            analyzer.run()

            summary = json.loads(analyzer.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["profile"], "seta")
            self.assertEqual(summary["input_policy"], "jobs/**/job.log only")
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["task_blocking_event_count"], 1)
            self.assertEqual(
                summary["monitor_environment_events_by_type"],
                {"apt.apt-lock-permission-denied": 1},
            )


if __name__ == "__main__":
    unittest.main()
