"""Tests for the extracted Harbor controller boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harbor_controller.analyzer_dispatch import dispatch_analyzer_handover
from harbor_controller.executor import build_control_argv, execute_action
from harbor_controller.policy import decide_action


def _observation(
    status: str,
    reason: str,
    *,
    running: int = 0,
    unfinished: int = 1,
    stalled_duration_reached: bool = False,
) -> dict[str, object]:
    return {
        "benchmark_status": status,
        "status_reason": reason,
        "finished": 0,
        "running": running,
        "unclaimed_remaining": 1,
        "unfinished": unfinished,
        "stalled_duration_reached": stalled_duration_reached,
    }


class HarborControllerPolicyTest(unittest.TestCase):
    def test_observations_never_execute_control_without_user_decision(self) -> None:
        cases = [
            (_observation("running", "progressing", running=1), 0, "wait", []),
            (_observation("blocked", "abnormal_exit"), 0, "notify", ["restart"]),
            (_observation("completed", "completed", unfinished=0), 0, "wait", []),
            (
                _observation("running", "timeout_reached", running=1),
                0,
                "notify",
                ["wait", "stop"],
            ),
            (
                _observation(
                    "running",
                    "suspected_stalled",
                    running=1,
                    stalled_duration_reached=True,
                ),
                0,
                "notify",
                ["wait", "stop"],
            ),
            (_observation("blocked", "abnormal_exit"), 3, "notify", []),
        ]
        for observation, retries, expected, allowed in cases:
            with self.subTest(expected=expected, retries=retries):
                action = decide_action(
                    observation,
                    retry_count=retries,
                    max_retries=3,
                )
                self.assertEqual(action["type"], expected)
                self.assertEqual(action["allowed_decisions"], allowed)

    def test_stall_below_decision_threshold_waits(self) -> None:
        action = decide_action(
            _observation("running", "suspected_stalled", running=1),
            retry_count=0,
            max_retries=3,
        )
        self.assertEqual(action["type"], "wait")
        self.assertEqual(action["allowed_decisions"], [])

    def test_restart_is_not_offered_while_a_worker_is_live(self) -> None:
        action = decide_action(
            _observation("blocked", "abnormal_exit", running=1),
            retry_count=0,
            max_retries=3,
        )
        self.assertEqual(action["type"], "notify")
        self.assertEqual(action["allowed_decisions"], [])

    def test_stop_is_not_offered_without_a_live_worker(self) -> None:
        action = decide_action(
            _observation("running", "timeout_reached"),
            retry_count=0,
            max_retries=3,
        )
        self.assertEqual(action["type"], "wait")
        self.assertEqual(action["allowed_decisions"], [])

    def test_completed_observation_marks_controller_completed(self) -> None:
        action = decide_action(
            _observation("completed", "completed", unfinished=0),
            retry_count=0,
            max_retries=3,
        )
        self.assertEqual(action["controller_status"], "completed")


class HarborControllerExecutorTest(unittest.TestCase):
    def test_rejects_commands_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            argv, error = build_control_argv("/bin/true", Path(root), "restart")
            self.assertIsNone(argv)
            self.assertEqual(error, "restart_cmd_not_run_specific")

    def test_executes_real_run_local_restart_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            command = run_dir / "restart.sh"
            command.write_text("#!/usr/bin/env bash\nprintf 'restarted\\n'\ntouch restarted.marker\n")
            command.chmod(0o755)
            state: dict[str, object] = {"retry_count": 0}
            requested = {"type": "restart", "reason": "user_approved", "retry_count": 0}

            result = execute_action(
                requested,
                restart_cmd="restart.sh",
                stop_cmd=None,
                run_dir=run_dir,
                state=state,
                observation=_observation("blocked", "abnormal_exit"),
                history=[],
            )

            self.assertEqual(result.action["type"], "restart")
            self.assertEqual(result.action["control_exit_code"], 0)
            self.assertEqual(state["retry_count"], 1)
            self.assertEqual(result.control_stdout, "restarted\n")
            self.assertTrue((run_dir / "restarted.marker").is_file())
            self.assertEqual(result.history[0]["status"], "restart_executed")

    def test_missing_restart_command_becomes_notify(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            observation = _observation("blocked", "abnormal_exit")
            requested = {"type": "restart", "reason": "user_approved", "retry_count": 0}
            result = execute_action(
                requested,
                restart_cmd=None,
                stop_cmd=None,
                run_dir=Path(root),
                state={"retry_count": 0},
                observation=observation,
                history=[],
            )
            self.assertEqual(result.action["type"], "notify")
            self.assertEqual(
                result.action["reason"],
                "restart_needed_but_restart_cmd_missing",
            )

    def test_executes_real_run_local_stop_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            command = run_dir / "stop.sh"
            command.write_text("#!/usr/bin/env bash\nprintf 'stopped\\n'\ntouch stopped.marker\n")
            command.chmod(0o755)
            requested = {"type": "stop", "reason": "user_approved", "retry_count": 0}

            result = execute_action(
                requested,
                restart_cmd=None,
                stop_cmd="stop.sh",
                run_dir=run_dir,
                state={"retry_count": 0},
                observation=_observation("running", "timeout_reached", running=1),
                history=[],
            )

            self.assertEqual(result.action["type"], "stop")
            self.assertEqual(result.action["control_exit_code"], 0)
            self.assertEqual(result.control_stdout, "stopped\n")
            self.assertTrue((run_dir / "stopped.marker").is_file())


class HarborControllerAnalyzerDispatchTest(unittest.TestCase):
    def test_spools_each_terminal_fingerprint_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            latest = root_path / "analyzer-handover-latest.json"
            state: dict[str, object] = {}
            handover = {
                "run_id": "run-1",
                "should_run_analyzer": True,
                "tasks": [
                    {
                        "task_index": "1",
                        "task_name": "task-a",
                        "terminal_fingerprint": "sha256-task-a",
                    }
                ],
            }

            def write_json(path: Path | None, payload: dict[str, object]) -> None:
                if path is None:
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")

            dispatch_analyzer_handover(
                handover,
                latest_output=latest,
                state=state,
                write_json=write_json,
            )
            first_spool = list((root_path / "analyzer-handoffs").glob("*.json"))
            dispatch_analyzer_handover(
                handover,
                latest_output=latest,
                state=state,
                write_json=write_json,
            )

            self.assertEqual(len(first_spool), 1)
            self.assertEqual(
                list((root_path / "analyzer-handoffs").glob("*.json")),
                first_spool,
            )
            self.assertEqual(
                state["analyzer_spooled_terminal_fingerprints"],
                ["sha256-task-a"],
            )
            self.assertEqual(json.loads(latest.read_text())["run_id"], "run-1")


if __name__ == "__main__":
    unittest.main()
