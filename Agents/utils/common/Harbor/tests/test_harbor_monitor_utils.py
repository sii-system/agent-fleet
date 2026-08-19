import json
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import harbor_monitor_utils


class HarborMonitorUtilsTest(unittest.TestCase):
    def test_queue_stats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done.txt"
            failed = root / "failed.txt"
            done.write_text("1\ttask-a\t1.0\n2\ttask-b\t0.0\n", encoding="utf-8")
            failed.write_text("3\ttask-c\t\tTimeoutError\n", encoding="utf-8")

            self.assertEqual(harbor_monitor_utils.reward_stats(done), ["reward=1.0: 1", "reward=0.0: 1"])
            self.assertEqual(
                harbor_monitor_utils.success_stats(done, failed),
                ["success:      1", "fail:         2", "success_rate: 33.33%"],
            )
            self.assertEqual(harbor_monitor_utils.exception_stats(done, failed), ["TimeoutError: 1"])

    def test_environment_signal_stats(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            path.write_text(
                json.dumps({"monitor_environment_events_by_type": {"network": 2, "auth": 3}}),
                encoding="utf-8",
            )
            self.assertEqual(
                harbor_monitor_utils.environment_signal_stats(path),
                ["auth: 3", "network: 2"],
            )


if __name__ == "__main__":
    unittest.main()
