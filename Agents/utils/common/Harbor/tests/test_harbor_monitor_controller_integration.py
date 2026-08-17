"""CLI integration tests for monitor observations and controller actions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
MONITOR = HARBOR_DIR / "scripts" / "monitor.py"
CONTROLLER = HARBOR_DIR / "scripts" / "controller.py"


class MonitorControllerIntegrationTest(unittest.TestCase):
    def _run_monitor(
        self,
        root: Path,
        *,
        extra_args: list[str],
        done: str = "",
        failed: str = "",
    ) -> dict[str, object]:
        queue = root / "queue" / "claude-code"
        queue.mkdir(parents=True, exist_ok=True)
        (root / "tasks.txt").write_text("task-a\n", encoding="utf-8")
        (queue / "done.txt").write_text(done, encoding="utf-8")
        (queue / "failed.txt").write_text(failed, encoding="utf-8")
        (queue / "next_index").write_text("2\n", encoding="utf-8")
        monitor_dir = root / "monitor"
        command = [
            sys.executable,
            str(MONITOR),
            "--run-dir",
            str(root),
            "--queue-dir",
            str(queue),
            "--task-file",
            str(root / "tasks.txt"),
            "--output",
            str(monitor_dir / "monitor-latest.json"),
            "--user-report-output",
            str(monitor_dir / "user-notify-latest.json"),
            "--analyzer-handover-output",
            str(monitor_dir / "analyzer-handover-latest.json"),
            "--runner-action-output",
            str(monitor_dir / "runner-action-latest.json"),
            "--startup-grace",
            "0",
            *extra_args,
        ]
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        return json.loads((monitor_dir / "monitor-latest.json").read_text())

    def _decide(
        self,
        run_dir: Path,
        decision: str,
        *,
        wait_seconds: int | None = None,
    ) -> dict[str, object]:
        command = [
            sys.executable,
            str(CONTROLLER),
            "--run-dir",
            str(run_dir),
            "decide",
            decision,
        ]
        if wait_seconds is not None:
            command.extend(["--wait-seconds", str(wait_seconds)])
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stdout)

    def test_wait_action_for_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = self._run_monitor(
                Path(root),
                extra_args=["--total", "1", "--claimed", "1", "--remaining", "0", "--running", "1"],
            )
            self.assertEqual(output["action"]["type"], "wait")

    def test_timeout_without_stop_command_only_offers_wait(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = self._run_monitor(
                Path(root),
                extra_args=[
                    "--total",
                    "1",
                    "--claimed",
                    "1",
                    "--remaining",
                    "0",
                    "--running",
                    "1",
                    "--configured-timeout",
                    "0",
                ],
            )
            self.assertEqual(output["action"]["type"], "notify")
            self.assertEqual(output["status_reason"], "timeout_reached")
            self.assertEqual(output["action"]["allowed_decisions"], ["wait"])
            self.assertEqual(output["user_notify"]["allowed_decisions"], ["wait"])
            self.assertEqual(
                output["monitor_follow_decision"],
                "continue_awaiting_user_decision",
            )

    def test_timeout_wait_then_user_stop_executes_run_local_control(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            stop = run_dir / "stop.sh"
            stop.write_text("#!/usr/bin/env bash\ntouch stopped.marker\n", encoding="utf-8")
            stop.chmod(0o755)
            args = [
                "--total",
                "1",
                "--claimed",
                "1",
                "--remaining",
                "0",
                "--running",
                "1",
                "--configured-timeout",
                "0",
                "--stop-cmd",
                "stop.sh",
            ]
            notified = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(notified["action"]["type"], "notify")

            self._decide(run_dir, "wait", wait_seconds=60)
            waiting = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(waiting["action"]["type"], "wait")
            self.assertEqual(waiting["action"]["decision_status"], "executed")
            self.assertFalse((run_dir / "stopped.marker").exists())

            state_path = run_dir / ".monitor_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["deferred_user_wait"]["wait_until"] = 0
            state_path.write_text(json.dumps(state), encoding="utf-8")
            notified_again = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(notified_again["action"]["type"], "notify")

            self._decide(run_dir, "stop")
            stopped = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(stopped["action"]["type"], "stop")
            self.assertEqual(stopped["action"]["decision_status"], "executed")
            self.assertTrue(stopped["action"]["external_control_performed"])
            self.assertEqual(stopped["benchmark_status"], "stopped")
            self.assertEqual(stopped["status_reason"], "stopped_by_user")
            self.assertTrue((run_dir / "stopped.marker").is_file())

    def test_abnormal_exit_user_restart_executes_and_defers_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            restart = run_dir / "restart.sh"
            restart.write_text("#!/usr/bin/env bash\ntouch restart.marker\n", encoding="utf-8")
            restart.chmod(0o755)
            args = ["--restart-cmd", "restart.sh"]
            notified = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(notified["action"]["allowed_decisions"], ["restart"])
            self.assertFalse(notified["analyzer_handover"]["should_run_analyzer"])
            self.assertFalse((run_dir / "monitor" / "analyzer-handoffs").exists())

            self._decide(run_dir, "restart")
            restarted = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(restarted["action"]["type"], "restart")
            self.assertEqual(restarted["action"]["decision_status"], "executed")
            self.assertEqual(restarted["state"]["retry_count"], 1)
            self.assertTrue((run_dir / "restart.marker").is_file())
            self.assertFalse(restarted["analyzer_handover"]["should_run_analyzer"])

    def test_abnormal_exit_without_restart_command_has_no_decision_request(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = self._run_monitor(Path(root), extra_args=[])

            self.assertEqual(output["action"]["allowed_decisions"], [])
            self.assertFalse(output["action"]["decision_required"])
            self.assertEqual(output["action"]["controller_status"], "action_required")
            self.assertIsNone(output["action"].get("decision_request_id"))
            self.assertEqual(output["monitor_follow_decision"], "continue")

    def test_failed_final_restart_clears_restart_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            restart = run_dir / "restart.sh"
            restart.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            restart.chmod(0o755)
            args = ["--restart-cmd", "restart.sh", "--max-retries", "1"]
            notified = self._run_monitor(run_dir, extra_args=args)
            self.assertEqual(notified["action"]["allowed_decisions"], ["restart"])
            self.assertFalse(notified["analyzer_handover"]["should_run_analyzer"])

            self._decide(run_dir, "restart")
            failed = self._run_monitor(run_dir, extra_args=args)

            self.assertEqual(failed["action"]["type"], "notify")
            self.assertEqual(failed["action"]["decision_status"], "failed")
            self.assertEqual(failed["action"]["decision_error"], "restart_failed_exit_code=1")
            self.assertEqual(failed["action"]["allowed_decisions"], [])
            self.assertFalse(failed["action"]["decision_required"])
            self.assertEqual(failed["action"]["controller_status"], "action_required")
            self.assertIsNone(failed["action"].get("decision_request_id"))
            self.assertEqual(failed["state"]["retry_count"], 1)
            self.assertTrue(failed["analyzer_handover"]["should_run_analyzer"])

    def test_failed_stop_reports_error_and_offers_another_decision(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            stop = run_dir / "stop.sh"
            stop.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            stop.chmod(0o755)
            args = [
                "--total",
                "1",
                "--claimed",
                "1",
                "--remaining",
                "0",
                "--running",
                "1",
                "--configured-timeout",
                "0",
                "--stop-cmd",
                "stop.sh",
            ]
            self._run_monitor(run_dir, extra_args=args)
            self._decide(run_dir, "stop")
            failed = self._run_monitor(run_dir, extra_args=args)

            self.assertEqual(failed["action"]["type"], "notify")
            self.assertEqual(failed["action"]["decision_status"], "failed")
            self.assertEqual(failed["action"]["decision_error"], "stop_failed_exit_code=1")
            self.assertEqual(failed["action"]["allowed_decisions"], ["wait", "stop"])
            self.assertTrue(failed["action"]["decision_required"])
            self.assertEqual(
                failed["action"]["controller_status"], "awaiting_user_decision"
            )
            self.assertIsNotNone(failed["action"].get("decision_request_id"))

    def test_abnormal_exit_offers_restart_without_executing_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            command = run_dir / "restart.sh"
            command.write_text(
                "#!/usr/bin/env bash\nprintf 'controller-restarted\\n'\ntouch restart.marker\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            output = self._run_monitor(
                run_dir,
                extra_args=["--restart-cmd", "restart.sh"],
            )
            self.assertEqual(output["action"]["type"], "notify")
            self.assertEqual(output["action"]["allowed_decisions"], ["restart"])
            self.assertEqual(output["state"]["retry_count"], 0)
            self.assertFalse((run_dir / "restart.marker").exists())

    def test_completion_stops_monitor_without_executing_stop_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            command = run_dir / "stop.sh"
            command.write_text(
                "#!/usr/bin/env bash\nprintf 'controller-stopped\\n'\ntouch stop.marker\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            output = self._run_monitor(
                run_dir,
                extra_args=["--stop-cmd", "stop.sh"],
                done="1\ttask-a\t1.0\n",
            )
            self.assertEqual(output["action"]["type"], "wait")
            self.assertEqual(output["monitor_follow_decision"], "stop_completed")
            self.assertEqual(output["user_notify"]["controller_status"], "completed")
            self.assertFalse((run_dir / "stop.marker").exists())


if __name__ == "__main__":
    unittest.main()
