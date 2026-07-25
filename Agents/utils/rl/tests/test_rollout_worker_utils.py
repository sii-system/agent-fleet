"""Tests for rollout-only worker maintenance helpers."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "rollout_worker_utils.py"
SPEC = importlib.util.spec_from_file_location("rollout_worker_utils", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RolloutWorkerUtilsTest(unittest.TestCase):
    def test_prune_trial_artifacts_keeps_newest_directories(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            worker_root = Path(root)
            for index in range(4):
                trial = worker_root / f"trial-{index}"
                trial.mkdir()
                (trial / "result.json").write_text("{}", encoding="utf-8")
                os.utime(trial, ns=(index + 1, index + 1))

            MODULE.prune_trial_artifacts(worker_root, keep=2)

            self.assertEqual(
                sorted(path.name for path in worker_root.iterdir()),
                ["trial-2", "trial-3"],
            )


if __name__ == "__main__":
    unittest.main()
