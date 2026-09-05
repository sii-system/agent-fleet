from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from process_rollout import (  # noqa: E402
    configure_managed_judge_python,
    resolve_ground_truth,
)


class ProcessRolloutTest(unittest.TestCase):
    def test_defaults_to_agent_fleet_private_cache(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"BROWSECOMP_CACHE_ROOT": root},
            clear=True,
        ):
            self.assertEqual(
                resolve_ground_truth(),
                Path(root) / "private" / "browsecomp_plus_decrypted.jsonl",
            )

    def test_explicit_ground_truth_wins(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"BROWSECOMP_GROUND_TRUTH": str(Path(root) / "gold.jsonl")},
            clear=True,
        ):
            self.assertEqual(resolve_ground_truth(), Path(root) / "gold.jsonl")

    def test_uses_managed_runtime_for_judge(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"BROWSECOMP_CACHE_ROOT": root},
            clear=True,
        ):
            python = Path(root) / "runtime" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            configure_managed_judge_python()
            self.assertEqual(os.environ["BROWSECOMP_JUDGE_PYTHON"], str(python))


if __name__ == "__main__":
    unittest.main()
