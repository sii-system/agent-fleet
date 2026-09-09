import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CLEANUP = Path(__file__).resolve().parents[1] / "harbor_nightly_cleanup.py"


@unittest.skipUnless(sys.platform == "linux", "requires Linux process identities")
class CleanupTest(unittest.TestCase):
    def test_stops_run_processes_and_orphan_pi_but_preserves_other_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processes = []
            child_record = root / "child.pid"
            try:
                for name, env in (
                    ("monitor", {"RUN_ID": "nightly-test", "OUTPUT_PATH": str(root)}),
                    ("analyzer", {"RUN_ID": "nightly-test", "OUTPUT_PATH": str(root)}),
                    (
                        "pi",
                        {
                            "PI_CODING_AGENT_DIR": str(
                                root / "analyzer/.pi-analyzer-home/task"
                            )
                        },
                    ),
                    (
                        "other",
                        {"RUN_ID": "another-run", "OUTPUT_PATH": str(root / "other")},
                    ),
                ):
                    ready = root / name
                    code = (
                        "import signal,time; from pathlib import Path; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"Path({str(ready)!r}).touch(); time.sleep(60)"
                    )
                    if name == "analyzer":
                        child_code = (
                            "import os,signal,time; from pathlib import Path; "
                            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                            f"Path({str(child_record)!r}).write_text(str(os.getpid())); time.sleep(60)"
                        )
                        code = (
                            "import subprocess; "
                            f"subprocess.Popen({[sys.executable, '-c', child_code]!r}, env={{}}, start_new_session=True); "
                        ) + code
                    processes.append(
                        subprocess.Popen(
                            [sys.executable, "-c", code],
                            env={**os.environ, **env},
                            start_new_session=True,
                        )
                    )
                deadline = time.monotonic() + 5
                while not all(
                    (root / name).exists()
                    for name in ("monitor", "analyzer", "pi", "other")
                ):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                while not child_record.exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(CLEANUP),
                        "--run-dir",
                        str(root),
                        "--run-id",
                        "nightly-test",
                        "--grace-seconds",
                        "0.1",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, "RUN_ID": "nightly-test", "OUTPUT_PATH": str(root)},
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for process in processes[:3]:
                    process.wait(timeout=3)
                self.assertIsNone(processes[3].poll())
                child_stat = Path(f"/proc/{child_record.read_text()}/stat")
                if child_stat.exists():
                    self.assertEqual(
                        child_stat.read_text().rsplit(")", 1)[1].split()[0], "Z"
                    )
            finally:
                for process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if child_record.exists():
                    try:
                        os.kill(int(child_record.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
