"""Tests for deterministic verification selection and rerun helpers."""

from __future__ import annotations

import os
import shlex
import signal
import sys
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
HARBOR_DIR = TEST_DIR.parent
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, make_exec_result, make_fix_plan
from harbor_fixer.validation import ValidationError, task_key
from harbor_fixer.verification.outcomes import aggregate_status, exec_failure_reason
from harbor_fixer.verification.rerun import (
    GENERATED_MONITOR_FILES,
    RUN_SCOPED_ENV_VARS,
    _terminate_process_group,
    map_run_records,
    run_command,
    wait_for_monitor,
)
from harbor_fixer.verification.selection import build_smoke_selection, plan_exec_map


def _fix_plan(count: int = 3) -> dict:
    plan = make_fix_plan()
    plan["plans"][0]["task_list"] = [
        {
            "task_index": str(index),
            "task_name": f"task-{index}",
            "attempt_id": None,
            "root_cause_code": "fixture",
            "final_class": "env_fail",
        }
        for index in range(1, count + 1)
    ]
    plan["plans"][0]["verification_hint"]["target_task_indexes"] = [
        str(index) for index in range(1, count + 1)
    ]
    return plan


class HarborFixerVerificationRuntimeTest(FixerTestCase):
    def _run_local_start(
        self, agent: str, tasks: list[str], **env_overrides: str
    ) -> dict:
        task_source = self.root / "tasks.txt"
        task_source.write_text("".join(f"{task}\n" for task in tasks), encoding="utf-8")
        dataset = self.root / "dataset"
        for task in tasks:
            task_dir = dataset / task
            task_dir.mkdir(parents=True)
            (task_dir / "task.yaml").write_text("version: 1\n", encoding="utf-8")
        env = {
            "API_KEY": "fake-key",
            "BASE_URL": "https://gateway.example.invalid",
            "DATASET_NAME": "auto",
            "DATASET_PATH": str(dataset),
            "MODEL": "fake-model",
            "HARBOR_DRY_RUN": "1",
            "TRACE_TO_OPIK": "false",
            **env_overrides,
        }
        with mock.patch.dict(os.environ, env):
            return run_command(
                shlex.join(["bash", str(HARBOR_DIR / "start.sh")]),
                self.root / "run",
                agent,
                task_source_path=str(task_source),
                selection_path=str(self.root / "selection.json"),
                should_run=True,
                timeout_seconds=10,
            )

    def test_outcomes_distinguish_policy_and_execution_failures(self) -> None:
        self.assertEqual(exec_failure_reason("failed", "denied"), "policy_denied")
        self.assertEqual(exec_failure_reason("failed", "allowed"), "execution_failed")
        self.assertEqual(aggregate_status(["fixed", "not_fixed"], 0), "partially_fixed")

    def test_operator_agent_config_is_not_classified_as_run_scoped(self) -> None:
        self.assertNotIn("AGENT", RUN_SCOPED_ENV_VARS)

    def test_selection_is_stable_and_maps_full_task_identity(self) -> None:
        plan = _fix_plan()
        exec_plans = plan_exec_map(make_exec_result())

        first = build_smoke_selection(
            plan, exec_plans, limit_per_plan=2, output_dir=self.root / "first"
        )
        second = build_smoke_selection(
            plan, exec_plans, limit_per_plan=2, output_dir=self.root / "second"
        )

        selected = first["tasks"]
        self.assertEqual(len(selected), 2)
        self.assertEqual(
            [task["selection_hash"] for task in selected],
            [task["selection_hash"] for task in second["tasks"]],
        )
        task = selected[0]
        record = {
            "task_index": task["smoke_task_index"],
            "task_name": task["task_name"],
            "task_complete_status": "complete_success",
        }
        mapped, unexpected, errors = map_run_records(
            {task["smoke_task_index"]: record},
            {**first, "tasks": [task]},
        )
        identity = {
            "task_index": task["original_task_index"],
            "task_name": task["task_name"],
            "attempt_id": task["attempt_id"],
        }
        self.assertIn(task_key(identity), mapped)
        self.assertEqual((unexpected, errors), ([], []))

    def test_selection_uses_unique_runnable_task_names(self) -> None:
        plan = _fix_plan()
        for index, task in enumerate(plan["plans"][0]["task_list"], start=1):
            task.update(task_name="same-task", attempt_id=f"attempt-{index}")

        selection = build_smoke_selection(
            plan,
            plan_exec_map(make_exec_result()),
            limit_per_plan=3,
            output_dir=self.root / "unique",
        )

        self.assertEqual(
            [task["task_name"] for task in selection["tasks"]], ["same-task"]
        )

    def test_run_command_clears_inherited_run_paths(self) -> None:
        task_source = self.root / "tasks.txt"
        task_source.write_text("task-b\ntask-a\n", encoding="utf-8")
        stale = {
            "QUEUE_DIR": "/stale/queue",
            "FLEET_TASKS": "old-task",
            "INCLUDE_TASKS": "old-task",
            "HARBOR_LIMIT": "1",
            "HARBOR_RUNS": "3",
            "N_ATTEMPTS": "4",
            "MIN_TEST": "1",
            "AGENT": "claude-code",
            "HARBOR_AGENT_IMPORT_PATH": "/stale/agent.py",
            "HARBOR_N_CONCURRENT": "1",
            "HARBOR_TASK_ID": "old-task",
            "HARBOR_ANALYZER_OUTPUT_DIR": "/stale/analyzer",
            "HARBOR_QUEUE_WORKER": "1",
            "HARBOR_ZELLIJ_SESSION_NAME": "original-run",
        }
        process = mock.Mock(returncode=0)
        process.wait.return_value = 0
        with (
            mock.patch.dict(os.environ, stale),
            mock.patch(
                "harbor_fixer.verification.rerun.subprocess.Popen",
                return_value=process,
            ) as popen,
        ):
            result = run_command(
                "verify",
                self.root / "run",
                "opencode",
                task_source_path=str(task_source),
                selection_path=str(self.root / "selection.json"),
                should_run=True,
                timeout_seconds=9,
            )

        self.assertEqual(result["exit_code"], 0)
        call = popen.call_args.kwargs
        env = call["env"]
        self.assertNotIn("QUEUE_DIR", env)
        self.assertNotIn("HARBOR_ANALYZER_OUTPUT_DIR", env)
        self.assertNotIn("HARBOR_ZELLIJ_SESSION_NAME", env)
        self.assertEqual(env["AGENT"], "opencode")
        self.assertEqual(env["HARBOR_AGENT_IMPORT_PATH"], "")
        self.assertEqual(env["HARBOR_QUEUE_WORKER"], "0")
        self.assertNotIn("HARBOR_N_CONCURRENT", env)
        self.assertNotIn("HARBOR_TASK_ID", env)
        self.assertEqual(env["FLEET_TASKS"], "task-b,task-a")
        self.assertEqual(env["INCLUDE_TASKS"], "task-b,task-a")
        self.assertEqual(env["HARBOR_LIMIT"], "")
        self.assertEqual(env["HARBOR_RUNS"], "1")
        self.assertEqual(env["N_ATTEMPTS"], "1")
        self.assertEqual(env["MIN_TEST"], "0")
        self.assertEqual(call["cwd"], self.root / "run")
        self.assertTrue(call["start_new_session"])
        self.assertTrue(call["stdout"].closed)
        self.assertTrue(call["stderr"].closed)
        process.wait.assert_called_once_with(timeout=9)
        self.assertEqual(
            (self.root / "run" / "tasks.txt").read_text(), "task-b\ntask-a\n"
        )

    def test_run_command_times_out(self) -> None:
        task_source = self.root / "tasks.txt"
        task_source.write_text("task-a\n", encoding="utf-8")
        term_marker = self.root / "term-received"
        verifier_backup = (
            self.root / "run" / "runtime" / "claude-code" / "verifier-uv.timeout"
        )
        verifier_backup.mkdir(parents=True)
        code = (
            "import signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, lambda *_: "
            "(Path(sys.argv[1]).write_text('term'), sys.exit(0))); time.sleep(10)"
        )

        result = run_command(
            shlex.join([sys.executable, "-c", code, str(term_marker)]),
            self.root / "run",
            "claude-code",
            task_source_path=str(task_source),
            selection_path=str(self.root / "selection.json"),
            should_run=True,
            timeout_seconds=0.2,
        )

        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed out after 0.2 seconds", result["stderr_summary"])
        self.assertEqual(term_marker.read_text(), "term")
        self.assertFalse(verifier_backup.exists())

    def test_run_command_rejects_invalid_commands_and_setup(self) -> None:
        task_source = self.root / "tasks.txt"
        task_source.write_text("task-a\n", encoding="utf-8")

        cases = (
            ('"', task_source, "invalid"),
            ("   ", task_source, "blank"),
            ("/missing/verification-command", task_source, "cannot launch"),
            ("true", self.root / "missing-tasks.txt", "cannot prepare"),
        )
        for command, source, message in cases:
            with self.subTest(command=command), self.assertRaisesRegex(
                ValidationError, message
            ):
                run_command(
                    command,
                    self.root / "run",
                    "claude-code",
                    task_source_path=str(source),
                    selection_path=str(self.root / "selection.json"),
                    should_run=True,
                    timeout_seconds=1,
                )

    def test_termination_kills_descendants_after_leader_exits(self) -> None:
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        with (
            mock.patch(
                "harbor_fixer.verification.rerun._process_group_exists",
                return_value=True,
            ),
            mock.patch(
                "harbor_fixer.verification.rerun.time.monotonic",
                side_effect=[0.0, 6.0],
            ),
            mock.patch("harbor_fixer.verification.rerun.os.killpg") as killpg,
        ):
            _terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [mock.call(123, signal.SIGTERM), mock.call(123, signal.SIGKILL)],
        )

    def test_wait_for_monitor_resets_generated_state(self) -> None:
        monitor_dir = self.root / "output" / "verification-monitor"
        monitor_dir.mkdir(parents=True)
        for name in GENERATED_MONITOR_FILES:
            (monitor_dir / name).write_text("stale\n", encoding="utf-8")

        snapshots = iter(
            [{"benchmark_status": "running"}, {"benchmark_status": "completed"}]
        )

        def snapshot(*_args: object, startup_grace: int) -> tuple[dict, str]:
            self.assertFalse(
                any((monitor_dir / name).exists() for name in GENERATED_MONITOR_FILES)
            )
            self.assertEqual(startup_grace, 1)
            return next(snapshots), str(
                monitor_dir / "monitor-latest.json"
            )

        with (
            mock.patch(
                "harbor_fixer.verification.rerun.generate_monitor_snapshot",
                side_effect=snapshot,
            ),
            mock.patch(
                "harbor_fixer.verification.rerun.time.monotonic",
                side_effect=[0.0, 0.0, 0.75, 0.75],
            ),
            mock.patch("harbor_fixer.verification.rerun.time.sleep") as sleep,
        ):
            monitor, _, timed_out = wait_for_monitor(
                self.root / "run",
                self.root / "output",
                "claude-code",
                timeout_seconds=1,
                poll_interval=30,
            )

        self.assertEqual(monitor, {"benchmark_status": "completed"})
        self.assertFalse(timed_out)
        sleep.assert_called_once_with(0.25)

    def test_verification_start_runs_directly_without_zellij(self) -> None:
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        zellij_marker = self.root / "zellij-called"
        fake_zellij = fake_bin / "zellij"
        fake_zellij.write_text(
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(zellij_marker))}\n",
            encoding="utf-8",
        )
        fake_zellij.chmod(0o755)
        result = self._run_local_start(
            "claude-code",
            ["fix-git"],
            PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        )

        self.assertEqual(result["exit_code"], 0, result["stderr_summary"])
        self.assertIn("HARBOR_DRY_RUN=1", result["stdout_summary"])
        self.assertNotIn("Zellij session", result["stdout_summary"])
        self.assertFalse(zellij_marker.exists())
        job_dir_file = self.root / "run" / "runtime" / "claude-code" / "harbor-job-dir"
        self.assertTrue(Path(job_dir_file.read_text().strip()).is_dir())
        exit_file = (
            self.root / "run" / "runtime" / "claude-code" / "harbor-benchmark.exit"
        )
        self.assertEqual(exit_file.read_text(), "0\n")
        self.assertEqual(list(exit_file.parent.glob("verifier-uv.*")), [])

    def test_verification_local_opencode_owns_concurrency(self) -> None:
        result = self._run_local_start(
            "opencode", ["task-a", "task-b"], HARBOR_N_CONCURRENT="8"
        )

        self.assertEqual(result["exit_code"], 0, result["stderr_summary"])
        self.assertIn("  --n-concurrent\n  8\n", result["stdout_summary"])


if __name__ == "__main__":
    import unittest

    unittest.main()
