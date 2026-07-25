"""Tests for Harbor worker helper utilities."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "harbor_worker_utils.py"
SPEC = importlib.util.spec_from_file_location("harbor_worker_utils", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HarborWorkerUtilsTest(unittest.TestCase):
    def test_finds_matching_task_blocking_online_event(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            events_path = Path(root) / "environment-events.jsonl"
            events_path.write_text(
                "\n".join(
                    [
                        json.dumps({"task_id": 2, "task_blocking": True, "event": "other-task"}),
                        json.dumps({"task_id": 1, "task_blocking": False, "event": "warning-only"}),
                        json.dumps({"task_id": 1, "task_blocking": True, "event": "apt-lock-permission-denied"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            reason = MODULE.online_early_stop_reason(events_path, 1)

            self.assertEqual(reason, "OnlineAnalysisEarlyStop:apt-lock-permission-denied")

    def test_online_early_stop_reason_cli_returns_nonzero_without_match(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            events_path = Path(root) / "environment-events.jsonl"
            events_path.write_text(
                json.dumps({"task_id": 3, "task_blocking": True, "event": "docker-build-step-failed"}) + "\n",
                encoding="utf-8",
            )

            found = subprocess.run(
                [sys.executable, str(SCRIPT), "online-early-stop-reason", str(events_path), "--task-id", "3"],
                check=False,
                stdout=subprocess.PIPE,
                text=True,
            )
            missing = subprocess.run(
                [sys.executable, str(SCRIPT), "online-early-stop-reason", str(events_path), "--task-id", "4"],
                check=False,
                stdout=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(found.returncode, 0)
            self.assertEqual(found.stdout.strip(), "OnlineAnalysisEarlyStop:docker-build-step-failed")
            self.assertNotEqual(missing.returncode, 0)
            self.assertEqual(missing.stdout, "")


if __name__ == "__main__":
    unittest.main()
