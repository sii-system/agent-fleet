"""Tests for Harbor monitor artifact resilience."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.scripts.harbor_monitor.artifacts import (
    load_harbor_job_snapshot,
    load_state,
    read_result_json,
)


class HarborMonitorArtifactsTest(unittest.TestCase):
    def test_native_snapshot_reads_nested_job_and_falls_back_to_trial_name(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            job_dir = Path(root)
            aggregate_path = job_dir / "wrapper" / "job" / "result.json"
            aggregate_path.parent.mkdir(parents=True)
            aggregate_path.write_text(
                json.dumps(
                    {
                        "n_total_trials": 1,
                        "stats": {
                            "n_running_trials": 0,
                            "n_pending_trials": 0,
                        },
                        "finished_at": "2026-07-28T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            trial_path = aggregate_path.parent / "trial-1" / "result.json"
            trial_path.parent.mkdir()
            trial_path.write_text(
                json.dumps(
                    {
                        "trial_name": "trial-1",
                        "exception_info": {"exception_type": "RuntimeError"},
                    }
                ),
                encoding="utf-8",
            )
            sidecar_path = job_dir / "sidecar" / "result.json"
            sidecar_path.parent.mkdir()
            sidecar_path.write_text(
                json.dumps({"stats": {"n_running_trials": 1}}),
                encoding="utf-8",
            )
            invalid_path = job_dir / "invalid" / "result.json"
            invalid_path.parent.mkdir()
            invalid_path.write_bytes(b"\xff")

            snapshot = load_harbor_job_snapshot(job_dir)

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.result_path, aggregate_path)
            self.assertEqual(snapshot.tasks["trial-1"].task_name, "trial-1")

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
