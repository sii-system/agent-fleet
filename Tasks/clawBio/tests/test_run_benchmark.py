import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run-benchmark.py"
SPEC = importlib.util.spec_from_file_location("clawbio_run_benchmark", MODULE_PATH)
run_benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = run_benchmark
SPEC.loader.exec_module(run_benchmark)


class ClawBioRunnerTest(unittest.TestCase):
    def test_clear_artifact_paths_removes_declared_outputs_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            outputs = workspace / "outputs"
            outputs.mkdir()
            (outputs / "report.md").write_text("stale", encoding="utf-8")
            keep = workspace / "input.txt"
            keep.write_text("keep", encoding="utf-8")

            run_benchmark.clear_artifact_paths(workspace, ["outputs"])

            self.assertFalse(outputs.exists())
            self.assertTrue(keep.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "chmod behavior is Linux-specific")
    def test_clear_artifact_paths_restores_write_permission_before_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            outputs = workspace / "outputs"
            outputs.mkdir()
            nested = outputs / "nested"
            nested.mkdir()
            (nested / "report.md").write_text("stale", encoding="utf-8")

            try:
                os.chmod(nested, 0o555)
                os.chmod(outputs, 0o555)
                run_benchmark.clear_artifact_paths(workspace, ["outputs"])
            finally:
                if nested.exists():
                    os.chmod(nested, 0o755)
                if outputs.exists():
                    os.chmod(outputs, 0o755)

            self.assertFalse(outputs.exists())

    def test_clear_artifact_paths_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            run_benchmark.clear_artifact_paths(Path(tmp), ["../outside"])

    def test_clear_artifact_paths_rejects_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            run_benchmark.clear_artifact_paths(Path(tmp), ["."])

    def test_task_session_id_is_stable_across_worker_assignment(self) -> None:
        started_at = datetime(2026, 5, 6, 3, 4, 5, 123456, tzinfo=timezone.utc)

        session_id = run_benchmark.task_session_id("turingdb-graph-demo", started_at)

        self.assertEqual(
            session_id,
            "clawbio-turingdb-graph-demo-20260506030405123456",
        )


if __name__ == "__main__":
    unittest.main()
