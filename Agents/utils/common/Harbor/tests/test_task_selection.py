import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
ENV_SH = HARBOR_DIR / "env.sh"
HARBOROPIK_SH = HARBOR_DIR / "harboropik.sh"
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
        env.pop("JUDGE_BASE_URL", None)
        env.pop("JUDGE_API_KEY", None)
        env.pop("JUDGE_MODEL", None)
        env.pop("HARBOR_DISALLOWED_TOOLS", None)
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
            "OPIK_URL": "",
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
                'printf "include=%s\\nharbor_include=%s\\n" '
                '"$INCLUDE_TASKS" "$HARBOR_INCLUDE_TASKS"'
            ),
            DATASET_NAME="terminalbench21",
            FLEET_TASKS="fix-git,break-filter-js-from-html",
            OPIK_URL="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("include=fix-git,break-filter-js-from-html", result.stdout)
        self.assertIn("harbor_include=fix-git,break-filter-js-from-html", result.stdout)

    def test_registry_selection_reports_every_missing_task(self) -> None:
        result = self.run_env(
            "harbor_prepare_registry_task_selection",
            DATASET_NAME="sweverify",
            FLEET_TASKS="missing-a,astropy__astropy-12907,missing-b",
            OPIK_URL="",
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
                OPIK_URL="",
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_deepsearchqa_alias_enables_native_web_tools(self) -> None:
        for dataset_name in (
            "deepsearchqa",
            "deepsearchqa@latest",
            "kgmon/deepsearchqa",
            "kgmon/deepsearchqa@latest",
        ):
            with self.subTest(dataset_name=dataset_name):
                result = self.run_env(
                    'printf "dataset=%s\\ntools=%s\\n" '
                    '"$(harbor_registry_dataset_name)" "$HARBOR_DISALLOWED_TOOLS"',
                    DATASET_NAME=dataset_name,
                    OPIK_URL="",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                expected_dataset = (
                    dataset_name.replace("deepsearchqa", "kgmon/deepsearchqa", 1)
                    if dataset_name.startswith("deepsearchqa")
                    else dataset_name
                )
                self.assertIn(f"dataset={expected_dataset}", result.stdout)
                self.assertIn("tools=RemoteTrigger AskUserQuestion", result.stdout)

    def test_deepsearchqa_preserves_explicit_disallowed_tools(self) -> None:
        for explicit_value in ("WebSearch Bash", ""):
            with self.subTest(explicit_value=explicit_value):
                result = self.run_env(
                    'printf "[%s]\\n" "$HARBOR_DISALLOWED_TOOLS"',
                    DATASET_NAME="deepsearchqa",
                    HARBOR_DISALLOWED_TOOLS=explicit_value,
                    OPIK_URL="",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"[{explicit_value}]")

    def test_empty_disallowed_tools_keeps_safe_default_for_other_datasets(self) -> None:
        result = self.run_env(
            'printf "[%s]\\n" "$HARBOR_DISALLOWED_TOOLS"',
            DATASET_NAME="terminalbench21",
            HARBOR_DISALLOWED_TOOLS="",
            OPIK_URL="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "[WebSearch WebFetch RemoteTrigger AskUserQuestion]",
        )

    def test_deepsearchqa_requires_every_judge_setting(self) -> None:
        valid_settings = {
            "JUDGE_BASE_URL": "https://judge.example/v1/chat/completions",
            "JUDGE_API_KEY": "test-key",
            "JUDGE_MODEL": "test-model",
        }
        for missing_name in valid_settings:
            for invalid_value in ("", " \t "):
                with self.subTest(
                    missing_name=missing_name,
                    invalid_value=invalid_value,
                ):
                    settings = valid_settings.copy()
                    settings[missing_name] = invalid_value
                    result = self.run_env(
                        "harbor_validate_dataset_runtime_requirements",
                        DATASET_NAME="deepsearchqa",
                        OPIK_URL="",
                        **settings,
                    )

                    self.assertEqual(result.returncode, 1)
                    self.assertIn(missing_name, result.stderr)
                    self.assertIn("config.local.env", result.stderr)

    def test_deepsearchqa_accepts_complete_judge_settings(self) -> None:
        result = self.run_env(
            "harbor_validate_dataset_runtime_requirements",
            DATASET_NAME="deepsearchqa",
            JUDGE_BASE_URL="https://judge.example/v1/chat/completions",
            JUDGE_API_KEY="test-key",
            JUDGE_MODEL="test-model",
            OPIK_URL="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deepsearchqa_live_runner_fails_before_creating_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            env = os.environ.copy()
            env.update(
                {
                    "DATASET_NAME": "deepsearchqa",
                    "JUDGE_BASE_URL": "",
                    "JUDGE_API_KEY": "",
                    "JUDGE_MODEL": "",
                    "HARBOR_DRY_RUN": "0",
                    "HARBOR_QUEUE_WORKER": "1",
                    "OPIK_URL": "",
                    "OUTPUT_PATH": str(output),
                    "QUEUE_DIR": str(output / "queue"),
                    "RUNTIME_DIR": str(output / "runtime"),
                    "JOBS_ROOT": str(output / "jobs"),
                }
            )

            result = subprocess.run(
                ["bash", str(HARBOROPIK_SH)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("JUDGE_BASE_URL", result.stderr)
            self.assertFalse(output.exists())

    def test_other_registry_datasets_do_not_require_a_judge_key(self) -> None:
        result = self.run_env(
            "harbor_validate_dataset_runtime_requirements",
            DATASET_NAME="owner/dataset",
            OPIK_URL="",
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

    def test_concurrent_prepare_generates_task_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            env = os.environ.copy()
            env.update(common, FLEET_TASKS="")
            command = (
                '. "$1"; mkdir -p "$QUEUE_DIR" "$RUNTIME_DIR"; '
                "harbor_generate_task_file() { "
                'echo generate >> "$OUTPUT_PATH/generate.calls"; sleep 0.2; '
                'echo task-a > "$TASK_FILE"; }; '
                "harbor_prepare_task_file"
            )
            processes = [
                subprocess.Popen(
                    ["bash", "-c", command, "bash", str(ENV_SH)],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(8)
            ]

            for process in processes:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr or stdout)
            calls = (output / "generate.calls").read_text(encoding="utf-8")
            self.assertEqual(calls.splitlines(), ["generate"])

    def test_missing_generic_dataset_explains_removed_auto_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, common = self.local_fixture(Path(tmp))

            result = self.run_env("harbor_ensure_dataset", **common)

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "automatic TerminalBench dataset cloning was removed",
                result.stderr,
            )
            self.assertIn("set DATASET_PATH", result.stderr)

    def test_reset_removes_generated_benchmark_and_fixer_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            analyzer_dir = output / "analyzer"
            summary_dir = analyzer_dir / "benchmark-summary"
            fixer_dir = output / "fixer"
            summary_dir.mkdir(parents=True)
            fixer_dir.mkdir()
            (analyzer_dir / "benchmark-summary.md").write_text("old summary\n", encoding="utf-8")
            (summary_dir / "summary-input.json").write_text("{}\n", encoding="utf-8")
            (fixer_dir / "fix-report-latest.md").write_text(
                "old fixer report\n",
                encoding="utf-8",
            )
            unrelated = analyzer_dir / "keep.txt"
            unrelated.write_text("keep\n", encoding="utf-8")
            fixer_unrelated = fixer_dir / "keep.json"
            fixer_unrelated.write_text("{}\n", encoding="utf-8")

            result = self.run_env('mkdir -p "$QUEUE_DIR"; harbor_reset_run_state', **common)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((analyzer_dir / "benchmark-summary.md").exists())
            self.assertFalse(summary_dir.exists())
            self.assertFalse((fixer_dir / "fix-report-latest.md").exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(fixer_unrelated.exists())

    def test_reset_removes_fixer_control_state_but_keeps_fixer_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            fixer_dir = output / "fixer"
            fixer_dir.mkdir(parents=True)
            for name in (
                "fixer-state.json",
                "fixer-control-request.json",
                "fixer-approval-request.json",
                "fixer-user-decision.json",
            ):
                (fixer_dir / name).write_text("{}\n", encoding="utf-8")
            result_file = fixer_dir / "exec-result-latest.json"
            result_file.write_text("{}\n", encoding="utf-8")

            result = self.run_env('mkdir -p "$QUEUE_DIR"; harbor_reset_run_state', **common)

            self.assertEqual(result.returncode, 0, result.stderr)
            for name in (
                "fixer-state.json",
                "fixer-control-request.json",
                "fixer-approval-request.json",
                "fixer-user-decision.json",
            ):
                self.assertFalse((fixer_dir / name).exists())
            self.assertTrue(result_file.exists())

    def test_reset_refuses_live_fixer_before_removing_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            fixer_dir = output / "fixer"
            monitor_dir = output / "monitor"
            fixer_dir.mkdir(parents=True)
            monitor_dir.mkdir()
            start_ticks = int(
                Path(f"/proc/{os.getpid()}/stat")
                .read_text(encoding="utf-8")
                .rsplit(")", 1)[1]
                .split()[19]
            )
            state_path = fixer_dir / "fixer-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "fixer_workflow_id": "fixer-live",
                        "status": "planning",
                        "owner": {
                            "pid": os.getpid(),
                            "start_ticks": start_ticks,
                        },
                    }
                ),
                encoding="utf-8",
            )
            monitor_marker = monitor_dir / "keep.json"
            monitor_marker.write_text("{}\n", encoding="utf-8")

            result = self.run_env(
                'mkdir -p "$QUEUE_DIR"; harbor_reset_run_state', **common
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to reset run state", result.stderr)
            self.assertTrue(state_path.exists())
            self.assertTrue(monitor_marker.exists())

    def test_reset_holds_fixer_lock_until_cleanup_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, common = self.local_fixture(Path(tmp), "task-a")
            marker = output / "reset-entered"
            env = os.environ.copy()
            env.pop("RESET_RUN", None)
            env.update(common)
            command = (
                '. "$1"; mkdir -p "$QUEUE_DIR"; '
                "harbor_stop_analyzer_supervisor() { "
                ': > "$OUTPUT_PATH/reset-entered"; sleep 1; }; '
                "harbor_reset_run_state"
            )
            process = subprocess.Popen(
                ["bash", "-c", command, "bash", str(ENV_SH)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())

            probe = subprocess.run(
                ["flock", "-n", str(output / "fixer" / ".fixer-control.lock"), "true"],
                check=False,
            )
            stdout, stderr = process.communicate(timeout=5)

            self.assertEqual(probe.returncode, 1)
            self.assertEqual(process.returncode, 0, stderr or stdout)

    def test_shared_process_terminator_stops_process(self) -> None:
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

        self.addCleanup(cleanup)

        result = self.run_env(
            f"harbor_terminate_validated_process {process.pid} 1",
            OPIK_URL="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        process.wait(timeout=2)
        self.assertLess(process.returncode, 0)
        self.assertEqual(
            ENV_SH.read_text(encoding="utf-8").count(
                "harbor_terminate_validated_process"
            ),
            4,
        )

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
                    "OPIK_URL": "",
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
