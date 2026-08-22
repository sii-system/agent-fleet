import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run-benchmark.py"
WRAPPER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run-openclaw-clawbio.sh"
)
SPEC = importlib.util.spec_from_file_location("clawbio_run_benchmark", MODULE_PATH)
run_benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = run_benchmark
SPEC.loader.exec_module(run_benchmark)


class ClawBioRunnerTest(unittest.TestCase):
    @staticmethod
    def run_wrapper(
        env_overrides: dict[str, str],
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for setting in (
            "SANDBOX_MODE",
            "EXEC_SECURITY",
            "EXEC_ASK",
            "WORKSPACE_ONLY",
        ):
            env.pop(setting, None)
        env.update(env_overrides)
        return subprocess.run(
            [str(WRAPPER_PATH), *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def sample_tasks() -> list[dict[str, str]]:
        return [
            {"id": "task-a", "prompt": "A"},
            {"id": "task-b", "prompt": "B"},
            {"id": "task-c", "prompt": "C"},
        ]

    def test_filter_tasks_validates_exact_ids_and_preserves_order(self) -> None:
        selected = run_benchmark.filter_tasks(
            self.sample_tasks(),
            " task-c,task-a,task-c ",
        )

        self.assertEqual([task["id"] for task in selected], ["task-c", "task-a"])

    def test_filter_tasks_reports_every_missing_id(self) -> None:
        with self.assertRaisesRegex(
            SystemExit,
            r"unknown ClawBio task\(s\): missing-a, missing-b",
        ):
            run_benchmark.filter_tasks(
                self.sample_tasks(),
                "task-a,missing-a,missing-b",
            )

    def test_wrapper_rejects_unknown_tasks_without_creating_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "tasks.json"
            run_root = root / "run"
            config.write_text(
                '{"defaults":{},"tasks":[{"id":"task-a","prompt":"A"}]}',
                encoding="utf-8",
            )
            result = self.run_wrapper(
                {
                    "TASK_CONFIG": str(config),
                    "RUN_ROOT": str(run_root),
                    "OPIK_URL": "",
                },
                "--tasks",
                "missing-a,missing-b",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "unknown ClawBio task(s): missing-a, missing-b",
                result.stderr,
            )
            self.assertFalse(run_root.exists())

            valid = self.run_wrapper(
                {
                    "TASK_CONFIG": str(config),
                    "RUN_ROOT": str(run_root),
                    "SANDBOX_MODE": "all",
                    "EXEC_SECURITY": "deny",
                    "EXEC_ASK": "always",
                    "WORKSPACE_ONLY": "true",
                },
                "--tasks",
                "task-a",
                "--validate-tasks-only",
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertFalse(run_root.exists())

    def test_wrapper_rejects_restrictive_security_before_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "run"
            result = self.run_wrapper(
                {
                    "RUN_ROOT": str(run_root),
                    "OPIK_URL": "",
                    "SANDBOX_MODE": "non-main",
                    "EXEC_SECURITY": "deny",
                    "EXEC_ASK": "always",
                    "WORKSPACE_ONLY": "true",
                }
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("security preflight failed before fleet setup", result.stderr)
            for setting in (
                "SANDBOX_MODE",
                "EXEC_SECURITY",
                "EXEC_ASK",
                "WORKSPACE_ONLY",
            ):
                self.assertIn(setting, result.stderr)
            self.assertFalse(run_root.exists())

    def test_wrapper_loads_dedicated_benchmark_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "tasks.json"
            run_root = root / "run"
            config.write_text(
                '{"defaults":{},"tasks":[{"id":"task-a","prompt":"A"}]}',
                encoding="utf-8",
            )
            # The committed profile supplies the permissive settings, so the
            # preflight passes and only warns. Stop at task validation so the
            # assertion does not depend on a Docker build.
            result = self.run_wrapper(
                {
                    "TASK_CONFIG": str(config),
                    "RUN_ROOT": str(run_root),
                    "OPIK_URL": "",
                    "DOCKER_COMPOSE_READ_ONLY": "true",
                },
                "--tasks",
                "task-a",
                "--validate-tasks-only",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("security preflight failed", result.stderr)
            self.assertFalse(run_root.exists())

    def test_wrapper_tolerates_retired_opik_switches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "tasks.json"
            config.write_text(
                '{"defaults":{},"tasks":[{"id":"task-a","prompt":"A"}]}',
                encoding="utf-8",
            )
            result = self.run_wrapper(
                {
                    "TASK_CONFIG": str(config),
                    "RUN_ROOT": str(root / "run"),
                    "OPIK_URL": "",
                    "TRACE_TO_OPIK": "false",
                },
                "--tasks",
                "task-a",
                "--validate-tasks-only",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TRACE_TO_OPIK is no longer used", result.stderr)

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
