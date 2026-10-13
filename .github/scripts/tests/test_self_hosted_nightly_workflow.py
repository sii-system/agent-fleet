import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from workflow_helpers import step_script

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "harbor-self-hosted-nightly.yml"
SELECTOR = ROOT / ".github" / "scripts" / "harbor_nightly_select.py"

SPEC = importlib.util.spec_from_file_location("harbor_nightly_select", SELECTOR)
assert SPEC and SPEC.loader
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)


class FakeTaskId:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class FakeClient:
    async def get_dataset_metadata(self, _benchmark):
        return SimpleNamespace(
            task_ids=[FakeTaskId("publisher/task-a"), FakeTaskId("publisher/task-b")]
        )


class FakeRandom:
    def sample(self, values, count):
        self.count = count
        return values[:count]


class SelectorTest(unittest.TestCase):
    def test_reads_local_task_lists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_list = root / selector.LOCAL_TASK_LISTS["seta"]
            task_list.parent.mkdir(parents=True)
            task_list.write_text(
                "# comment\ntask-a\ntask-b\ntask-a\n", encoding="utf-8"
            )
            self.assertEqual(selector.task_names(root, "seta"), ["task-a", "task-b"])

    def test_reads_harbor_machine_metadata(self):
        self.assertEqual(
            selector.registry_task_names("publisher/dataset", FakeClient),
            ["publisher/task-a", "publisher/task-b"],
        )

    def test_selects_twenty_unique_tasks(self):
        available = [f"task-{index}" for index in range(20)]
        randomizer = FakeRandom()
        with mock.patch.object(selector, "task_names", return_value=available):
            selected = selector.select_tasks(Path("."), "seta", randomizer)
        self.assertEqual(len(selected), 20)
        self.assertEqual(len(set(selected)), 20)
        self.assertEqual(randomizer.count, 20)

    def test_rejects_benchmarks_with_fewer_than_twenty_tasks(self):
        available = [f"task-{index}" for index in range(19)]
        with (
            mock.patch.object(selector, "task_names", return_value=available),
            self.assertRaisesRegex(RuntimeError, "need 20"),
        ):
            selector.select_tasks(Path("."), "seta", FakeRandom())

    def test_explicit_replay_preserves_order_and_removes_duplicates(self):
        with mock.patch.object(selector, "task_names", return_value=["task-a", "task-b"]):
            self.assertEqual(
                selector.requested_tasks(Path("."), "seta", " task-b,task-a,task-b "),
                ["task-b", "task-a"],
            )

    def test_explicit_replay_rejects_invalid_or_excess_tasks(self):
        for tasks in ("unknown", ",,", ",".join(f"task-{i}" for i in range(21))):
            with self.subTest(tasks=tasks), mock.patch.object(
                selector, "task_names", return_value=["task-a"]
            ), self.assertRaises(ValueError):
                selector.requested_tasks(Path("."), "seta", tasks)


class WorkflowTest(unittest.TestCase):
    def step_script(self, name):
        return step_script(WORKFLOW, name)

    def test_dispatch_validates_replay_parameters_before_launch(self):
        script = self.step_script("Select benchmark and tasks")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_python = root / "selector"
            fake_python.write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\nprintf "1\\nrequested-task\\n"\n'
            )
            fake_python.chmod(0o755)
            for benchmark, tasks, expected in (
                ("sierra-research/tau3-bench", "requested-task", 0),
                ("terminal-bench/terminal-bench-2-1", "", 1),
                ("", "requested-task", 1),
            ):
                with self.subTest(benchmark=benchmark, tasks=tasks):
                    output = root / "output"
                    output.write_text("")
                    result = subprocess.run(
                        ["bash", "-c", script],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                        env={
                            **os.environ,
                            "INPUT_AGENT": "claude-code",
                            "INPUT_BENCHMARK": benchmark,
                            "INPUT_TASKS": tasks,
                            "SMITH_DATASET_PATH": str(root / "absent"),
                            "HARBOR_NIGHTLY_PYTHON": str(fake_python),
                            "HARBOR_NIGHTLY_SELECTOR": str(SELECTOR),
                            "GITHUB_WORKSPACE": str(ROOT),
                            "GITHUB_OUTPUT": str(output),
                            "RUN_ID": "test-replay",
                        },
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if expected == 0:
                        self.assertIn(f"benchmark={benchmark}\n", output.read_text())
                        self.assertIn("task_count=1\n", output.read_text())

    def test_selector_survives_checkout_of_an_older_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".github/scripts/harbor_nightly_select.py"
            control.parent.mkdir(parents=True)
            control.write_text(SELECTOR.read_text())
            task_file = root / selector.LOCAL_TASK_LISTS["seta"]
            task_file.parent.mkdir(parents=True)
            task_file.write_text("task-a\n")
            (root / ".github/harbor-nightly-benchmarks.txt").write_text("seta\n")
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            env_file, output = root / "env", root / "output"
            env = {
                **os.environ, "RUNNER_TEMP": str(runner_temp),
                "GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(output),
                "GITHUB_WORKSPACE": str(root), "RUN_ID": "old-revision-test",
                "INPUT_AGENT": "claude-code", "INPUT_BENCHMARK": "seta",
                "INPUT_TASKS": "task-a", "HARBOR_NIGHTLY_PYTHON": sys.executable,
                "SMITH_DATASET_PATH": str(root / "absent"),
            }
            subprocess.run(
                ["bash", "-c", self.step_script("Preserve nightly task selector")],
                cwd=root, env=env, check=True, capture_output=True,
            )
            name, value = env_file.read_text().strip().split("=", 1)
            env[name] = value
            control.write_text('raise SystemExit("old selector has no --tasks")\n')
            result = subprocess.run(
                ["bash", "-c", self.step_script("Select benchmark and tasks")],
                cwd=root, env=env, check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tasks=task-a\n", output.read_text())

    def test_smith_launches_only_sampled_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "script"
            launcher.write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                'printf "%s\\n" "$*" "$HARBOR_INCLUDE_TASKS" > "$CAPTURE"\n'
            )
            launcher.chmod(0o755)
            capture = root / "capture"
            result = subprocess.run(
                ["bash", "-c", self.step_script("Run Harbor benchmark")],
                cwd=ROOT, capture_output=True, text=True, check=False,
                env={
                    **os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                    "API_KEY": "fake-key", "BASE_URL_RAW": "https://gateway.invalid/v1",
                    "BENCHMARK": "smith", "AGENT": "claude-code", "TOTAL_WORKERS": "10",
                    "HARBOR_INCLUDE_TASKS": "task-a,task-b", "CAPTURE": str(capture),
                    "OUTPUT_PATH": str(root), "GITHUB_OUTPUT": str(root / "output"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = capture.read_text()
            self.assertIn("--task task-a\\,task-b", captured)
            self.assertTrue(captured.endswith("\n\n"), captured)

    def test_health_reports_registry_and_smith_failures(self):
        script = self.step_script("Verify sampled tasks completed")
        for benchmark, done, failed, shell_status, expected_status in (
            ("registry", 20, 0, 0, 0),
            ("registry", 20, 0, 1, 1),
            ("smith", 20, 0, 0, 0),
            ("smith", 17, 3, 0, 1),
            ("smith", 19, 0, 0, 1),
            ("smith", 20, 0, 1, 1),
            ("smith", 0, 0, 0, 1),
        ):
            with (
                self.subTest(
                    benchmark=benchmark,
                    done=done,
                    failed=failed,
                    shell_status=shell_status,
                ),
                tempfile.TemporaryDirectory() as tmp,
            ):
                output = Path(tmp) / "run"
                output.mkdir()
                (output / "summary.txt").write_text(
                    "status: complete\nharbor_exit_code: 0\ntotal: 20\ncompleted: 20\nerrored: 0\n"
                )
                if done or failed:
                    queue = output / "queue" / "worker"
                    queue.mkdir(parents=True)
                    (queue / "done.txt").write_text("task\t0\tresult\t\n" * done)
                    (queue / "failed.txt").write_text("task\n" * failed)
                report = Path(tmp) / "health.md"
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    env={
                        **os.environ,
                        "BENCHMARK": benchmark,
                        "EXPECTED_TASKS": "20",
                        "OUTPUT_PATH": str(output),
                        "HEALTH_SUMMARY": str(report),
                        "HARBOR_STATUS": str(shell_status),
                    },
                )
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertTrue(report.exists(), result.stderr)
                self.assertIn(
                    "PASS" if expected_status == 0 else "FAIL", report.read_text()
                )

    def test_staging_redacts_saved_credentials_and_structured_overrides(self):
        script = self.step_script("Stage and redact artifacts")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.local.env").write_text(
                "EXA_API_KEY=fake-exa-key\n"
                "HARBOR_ANALYZER_API_KEY=fake-analyzer-key\n"
                "HARBOR_FIXER_API_KEY=fake-fixer-key\n"
                'HARBOR_LLM_KWARGS=\'{"api_key":"fake-llm-key"}\'\n'
                'OPENCODE_RUNTIME_SECRETS_JSON=\'{"custom_header":"fake-header-key"}\'\n'
            )
            original = b"fake-exa-key fake-analyzer-key fake-fixer-key fake-llm-key fake-header-key"
            output = root / "run"
            output.mkdir()
            (output / "agent.log").write_bytes(original)
            env_file = root / "env"
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "GITHUB_WORKSPACE": tmp,
                    "RUNNER_TEMP": tmp,
                    "OUTPUT_PATH": str(output),
                    "GITHUB_ENV": str(env_file),
                    "API_KEY": "fake-primary-key",
                    "OPIK_API_KEY": "",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            staged = Path(env_file.read_text().strip().split("=", 1)[1])
            self.assertEqual(
                (staged / "agent.log").read_bytes(), b"*** *** *** *** ***"
            )
            self.assertEqual((output / "agent.log").read_bytes(), original)

    def test_summary_renderer_failure_falls_back_to_health(self):
        script = self.step_script("Publish run summary")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.md").write_bytes(b"invalid utf8: \xff")
            health = root / "health.md"
            health.write_text("Validation: PASS\n")
            summary = root / "published.md"
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "HEALTH_SUMMARY": str(health),
                    "JOB_STATUS": "success",
                    "GATE_OUTCOME": "success",
                    "STAGING_OUTCOME": "success",
                    "STAGED_ARTIFACTS": tmp,
                    "ARTIFACT_OUTCOME": "success",
                    "ARTIFACT_URL": "https://example.com/artifact",
                    "RUN_URL": "https://example.com/run",
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Validation: PASS", summary.read_text())
            self.assertNotIn("<details>", summary.read_text())


if __name__ == "__main__":
    unittest.main()
