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
        queue.mkdir(parents=True)
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

    def test_wait_action_for_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = self._run_monitor(
                Path(root),
                extra_args=["--total", "1", "--claimed", "1", "--remaining", "0", "--running", "1"],
            )
            self.assertEqual(output["action"]["type"], "wait")

    def test_notify_action_for_live_worker_past_timeout(self) -> None:
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
            self.assertEqual(output["action"]["allowed_decisions"], ["wait", "stop"])
            self.assertEqual(output["user_notify"]["allowed_decisions"], ["wait", "stop"])

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
