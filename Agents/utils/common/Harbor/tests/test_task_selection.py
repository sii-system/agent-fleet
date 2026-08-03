import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
ENV_SH = HARBOR_DIR / "env.sh"
START_SH = HARBOR_DIR / "start.sh"
PREPARE_LOCAL = 'mkdir -p "$QUEUE_DIR" "$RUNTIME_DIR"; harbor_prepare_task_file'


class HarborTaskSelectionTest(unittest.TestCase):
    def run_env(
        self,
        command: str,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("RESET_RUN", None)
        env.update(overrides)
        return subprocess.run(
            ["bash", "-c", f'. "$1"; {command}', "bash", str(ENV_SH)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def local_fixture(
        self,
        root: Path,
        *task_ids: str,
    ) -> tuple[Path, dict[str, str]]:
        dataset = root / "dataset"
        output = root / "run"
        for task_id in task_ids:
            task_dir = dataset / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "task.yaml").write_text("version: 1\n", encoding="utf-8")
        return output, {
            "DATASET_NAME": "auto",
            "DATASET_PATH": str(dataset),
            "OUTPUT_PATH": str(output),
            "TASK_FILE": str(output / "tasks.txt"),
            "QUEUE_DIR": str(output / "queue"),
            "RUNTIME_DIR": str(output / "runtime"),
            "TRACE_TO_OPIK": "false",
        }

    def prepare_local(
        self,
        common: dict[str, str],
        tasks: str,
        *,
        reset: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_env(
            PREPARE_LOCAL,
            **common,
            FLEET_TASKS=tasks,
            **({"RESET_RUN": "1"} if reset else {}),
        )

    def test_registry_selection_is_exact_and_sets_include_tasks(self) -> None:
        result = self.run_env(
            (
                "harbor_prepare_registry_task_selection; "
                'printf "include=%s\\ntb_include=%s\\n" '
                '"$INCLUDE_TASKS" "$TB_INCLUDE_TASKS"'
            ),
            DATASET_NAME="terminalbench21",
            FLEET_TASKS="fix-git,break-filter-js-from-html",
            TRACE_TO_OPIK="false",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("include=fix-git,break-filter-js-from-html", result.stdout)
        self.assertIn("tb_include=fix-git,break-filter-js-from-html", result.stdout)

    def test_registry_selection_reports_every_missing_task(self) -> None:
        result = self.run_env(
            "harbor_prepare_registry_task_selection",
            DATASET_NAME="sweverify",
            FLEET_TASKS="missing-a,astropy__astropy-12907,missing-b",
            TRACE_TO_OPIK="false",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown task(s): missing-a, missing-b", result.stderr)

    def test_registry_selection_honors_configured_task_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_source = Path(tmp) / "tasks.txt"
            task_source.write_text("custom-task\n", encoding="utf-8")
            result = self.run_env(
                "harbor_prepare_registry_task_selection",
                DATASET_NAME="terminalbench21",
                TASK_SOURCE_FILE=str(task_source),
                FLEET_TASKS="custom-task",
                TRACE_TO_OPIK="false",
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_selection_filters_and_guards_run_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(
                Path(tmp), "task-a", "task-b", "task-c"
            )
            task_file = output / "tasks.txt"
            cases = (
                ("initial", "task-c,task-a", False, 0, "task-c\ntask-a\n"),
                ("same", "task-c,task-a", False, 0, "task-c\ntask-a\n"),
                ("implicit-resume", "", False, 0, "task-c\ntask-a\n"),
                ("mismatch", "task-b", False, 2, "task-c\ntask-a\n"),
                ("reset", "task-b", True, 0, "task-b\n"),
                ("reset-full", "", True, 0, "task-a\ntask-b\ntask-c\n"),
            )
            for label, tasks, reset, returncode, expected in cases:
                with self.subTest(label=label):
                    result = self.prepare_local(common, tasks, reset=reset)
                    self.assertEqual(result.returncode, returncode, result.stderr)
                    self.assertEqual(task_file.read_text(encoding="utf-8"), expected)
                    if label == "mismatch":
                        self.assertIn("RESET_RUN=1", result.stderr)

    def test_local_selection_reports_every_missing_task_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            result = self.prepare_local(common, "missing-a,task-a,missing-b")

            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown task(s): missing-a, missing-b", result.stderr)
            self.assertFalse((output / "tasks.txt").exists())

    def test_reset_removes_generated_benchmark_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            analyzer_dir = output / "analyzer"
            summary_dir = analyzer_dir / "benchmark-summary"
            summary_dir.mkdir(parents=True)
            (analyzer_dir / "benchmark-summary.md").write_text("old summary\n", encoding="utf-8")
            (summary_dir / "summary-input.json").write_text("{}\n", encoding="utf-8")
            unrelated = analyzer_dir / "keep.txt"
            unrelated.write_text("keep\n", encoding="utf-8")

            result = self.run_env('mkdir -p "$QUEUE_DIR"; harbor_reset_run_state', **common)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((analyzer_dir / "benchmark-summary.md").exists())
            self.assertFalse(summary_dir.exists())
            self.assertTrue(unrelated.exists())

    def test_start_passes_run_id_to_analyzer(self) -> None:
        self.assertIn('--run-id "$RUN_ID"', START_SH.read_text(encoding="utf-8"))

    def test_unknown_smith_task_fails_before_dataset_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "run"
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root),
                    "XDG_RUNTIME_DIR": str(root),
                    "AGENT_FLEET_PATHS_FILE": str(root / "missing-paths.env"),
                    "DATASET_NAME": "smith",
                    "DATASET_PATH": str(root / "missing-dataset"),
                    "SMITH_ADAPTER_DIR": str(root / "missing-adapter"),
                    "SMITH_GENERATE_IF_MISSING": "1",
                    "FLEET_TASKS": "missing-task",
                    "OUTPUT_PATH": str(output),
                    "TRACE_TO_OPIK": "false",
                }
            )

            result = subprocess.run(
                ["bash", str(START_SH)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown task(s): missing-task", result.stderr)
            self.assertNotIn("adapter not found", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
