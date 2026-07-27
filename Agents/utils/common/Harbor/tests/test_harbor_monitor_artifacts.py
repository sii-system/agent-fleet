"""Tests for Harbor monitor artifact resilience."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.scripts.harbor_monitor.artifacts import (
    load_state,
    read_result_json,
)


class HarborMonitorArtifactsTest(unittest.TestCase):
    def test_read_result_json_ignores_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result_path = Path(root) / "result.json"
            result_path.write_bytes(b"\xff")

            self.assertEqual(
                read_result_json(str(result_path), []),
                (False, None, None, None),
            )

    def test_load_state_resets_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "state.json"
            state_path.write_bytes(b"\xff")

            self.assertEqual(
                load_state(state_path),
                {"retry_count": 0, "history": [], "adaptive_S": None},
            )


if __name__ == "__main__":
    unittest.main()
