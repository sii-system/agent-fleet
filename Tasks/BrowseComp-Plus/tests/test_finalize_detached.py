from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1]
FINALIZER = BENCHMARK / "scripts" / "finalize_detached.py"


class DetachedFinalizerTest(unittest.TestCase):
    def test_collects_and_finishes_none_judge_after_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            trial = root_path / "jobs" / "q1__trial"
            agent = trial / "agent"
            agent.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({"task_name": "q1", "exception_info": None}),
                encoding="utf-8",
            )
            (agent / "pi.txt").write_text(
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Exact Answer: fixture"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = root_path / "summary.txt"
            summary.write_text("stale\n", encoding="utf-8")
            os.utime(summary, ns=(1, 1))
            summary_not_before_ns = time.time_ns()
            ground_truth = root_path / "gold.jsonl"
            ground_truth.write_text(
                json.dumps({"query_id": "q1", "query": "Q", "answer": "fixture"}) + "\n",
                encoding="utf-8",
            )
            status = root_path / "finalizer.json"
            manifest = root_path / "task-manifest.json"
            manifest.write_text(
                json.dumps({"task_count": 1, "tasks": [{"query_id": "q1"}]}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["BROWSECOMP_JUDGE_MODE"] = "none"
            process = subprocess.Popen(
                [
                    "python3",
                    str(FINALIZER),
                    "--summary-file",
                    str(summary),
                    "--summary-not-before-ns",
                    str(summary_not_before_ns),
                    "--jobs-root",
                    str(root_path / "jobs"),
                    "--official-run-dir",
                    str(root_path / "official"),
                    "--task-manifest",
                    str(manifest),
                    "--eval-dir",
                    str(root_path / "eval"),
                    "--source-root",
                    str(root_path / "source"),
                    "--ground-truth",
                    str(ground_truth),
                    "--status-file",
                    str(status),
                    "--timeout-seconds",
                    "2",
                    "--poll-seconds",
                    "0.01",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.1)
                self.assertIsNone(process.poll(), "finalizer accepted the stale summary")
                summary.write_text("current\n", encoding="utf-8")
                _, stderr = process.communicate(timeout=3)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(status.read_text())["state"], "completed")
            self.assertTrue((root_path / "official" / "q1.json").is_file())


if __name__ == "__main__":
    unittest.main()
