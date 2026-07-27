import json
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "fleet_batch.sh"
HARBOR_START = SCRIPT.parents[1] / "Agents/utils/common/Harbor/start.sh"
HARBOR_RUN_STATE_VARS = (
    "OUTPUT_PATH",
    "TASK_FILE",
    "QUEUE_DIR",
    "RUNTIME_DIR",
    "LAYOUT_FILE",
    "JOBS_ROOT",
    "HARBOR_ONLINE_ANALYSIS_DIR",
    "HARBOR_ONLINE_ANALYSIS_PID_FILE",
    "HARBOR_ONLINE_ANALYSIS_LOG_FILE",
    "HARBOR_MONITOR_DIR",
    "HARBOR_MONITOR_PID_FILE",
    "HARBOR_MONITOR_LOG_FILE",
    "HARBOR_BENCHMARK_PID_FILE",
    "HARBOR_BENCHMARK_EXIT_FILE",
    "HARBOR_JOB_DIR_FILE",
    "RL_TRACE_LOG",
    "RL_SERVER_LOG",
    "RL_SERVER_PID_FILE",
    "RL_QUEUE_DIR",
    "RL_ACTIVE_DIR",
    "RL_JOB_QUEUE_ROOT",
    "RL_JOB_RUNTIME_ROOT",
)


class FleetBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.artifact_root = self.root / "batch-artifacts"
        self.calls = self.root / "runner-calls.txt"

        harbor = self.repo / "Agents/utils/common/Harbor/start.sh"
        harbor.parent.mkdir(parents=True)
        harbor.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
{
  printf 'DATASET_NAME=%s RUN_ID=%s ZELLIJ_SESSION_NAME=%s FLEET_BATCH_HARBOR_RUNS=%s OUTPUT_PATH=<%s> TASK_FILE=<%s> QUEUE_DIR=<%s> RUNTIME_DIR=<%s> LAYOUT_FILE=<%s> JOBS_ROOT=<%s> args=%s\\n' \
    "${DATASET_NAME-}" "${RUN_ID-}" "${ZELLIJ_SESSION_NAME-}" \
    "${FLEET_BATCH_HARBOR_RUNS-}" \
    "${OUTPUT_PATH-<unset>}" "${TASK_FILE-<unset>}" "${QUEUE_DIR-<unset>}" \
    "${RUNTIME_DIR-<unset>}" "${LAYOUT_FILE-<unset>}" "${JOBS_ROOT-<unset>}" "$*"
} >>"$STUB_CALLS"
printf 'runner=harbor\\n'
printf 'DATASET_NAME=%s\\n' "${DATASET_NAME-}"
printf 'RUN_ID=%s\\n' "${RUN_ID-}"
printf 'ZELLIJ_SESSION_NAME=%s\\n' "${ZELLIJ_SESSION_NAME-}"
printf 'args=%s\\n' "$*"
[[ "${DATASET_NAME-}" != "fail/taskset" ]] || exit 7
""",
            encoding="utf-8",
        )

        pinchbench = self.repo / "Tasks/Pinchbench/scripts/run-parallel-workers.py"
        pinchbench.parent.mkdir(parents=True)
        pinchbench.write_text(
            """import os
import signal
import time
from pathlib import Path

pid_file = os.environ.get("STUB_CHILD_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")

    def stop(signum, frame):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    while True:
        time.sleep(0.05)

print("runner=pinchbench")
""",
            encoding="utf-8",
        )

        clawbio = self.repo / "Tasks/clawBio/scripts/run-openclaw-clawbio.sh"
        clawbio.parent.mkdir(parents=True)
        clawbio.write_text(
            """#!/usr/bin/env bash
printf 'runner=clawbio\\n'
if [[ -n "${STUB_GRANDCHILD_PID_FILE:-}" ]]; then
  # Mirror the real launcher shape: the benchmark work runs in a foreground
  # grandchild of the launcher shell.
  bash -c 'echo "$$" >"$STUB_GRANDCHILD_PID_FILE"; trap "exit 0" TERM INT HUP; while true; do sleep 0.05; done'
fi
""",
            encoding="utf-8",
        )
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://gateway.example.invalid\n"
            "API_KEY=fake-runner-key\n"
            "MODEL=test-model\n"
            "TRACE_TO_OPIK=false\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def batch_env(self):
        env = os.environ.copy()
        for name in (
            "RUN_ID",
            "ZELLIJ_SESSION_NAME",
            "DATASET_NAME",
            "BASE_URL",
            "API_KEY",
            "AUTH_TOKEN",
            "MODEL",
            "TRACE_TO_OPIK",
            "OPIK_URL",
            *HARBOR_RUN_STATE_VARS,
        ):
            env.pop(name, None)
        env.update(
            {
                "REPO_DIR": str(self.repo),
                "FLEET_BATCH_LOG_DIR": str(self.artifact_root),
                "STUB_CALLS": str(self.calls),
            }
        )
        return env

    def run_batch(self, *args, env_overrides=None):
        env = self.batch_env()
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_spec(self, name, taskset, **extra):
        path = self.root / name
        path.write_text(
            json.dumps({"schema_version": 1, "taskset": taskset, **extra}),
            encoding="utf-8",
        )
        return path

    def artifact_dir(self):
        dirs = list(self.artifact_root.iterdir())
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    def test_two_spec_files_launch_with_unique_run_and_session_ids(self):
        first = self.write_spec("first.json", "owner/first", agent="claude-code")
        second = self.write_spec("second.json", "owner/second", workers=2)

        result = self.run_batch("--spec", str(first), str(second))

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        run_ids = [re.search(r"RUN_ID=([^ ]+)", line).group(1) for line in calls]
        sessions = [
            re.search(r"ZELLIJ_SESSION_NAME=([^ ]+)", line).group(1)
            for line in calls
        ]
        self.assertEqual(len(set(run_ids)), 2)
        self.assertEqual(run_ids, sessions)
        self.assertTrue(
            all("FLEET_BATCH_HARBOR_RUNS=2" in line for line in calls)
        )
        self.assertTrue(all("args=--detach" in line for line in calls))
        self.assertIn("[1/2] owner/first", result.stderr)
        self.assertIn("[2/2] owner/second", result.stderr)

        artifacts = self.artifact_dir()
        self.assertEqual(
            json.loads((artifacts / "1.spec.json").read_text(encoding="utf-8")),
            {"schema_version": 1, "taskset": "owner/first", "agent": "claude-code"},
        )
        self.assertTrue((artifacts / "1.log").is_file())
        self.assertTrue((artifacts / "2.log").is_file())

    def test_array_file_launches_each_spec(self):
        batch = self.root / "runs.json"
        batch.write_text(
            json.dumps(
                [
                    {"schema_version": 1, "taskset": "owner/first"},
                    {"schema_version": 1, "taskset": "owner/second"},
                ]
            ),
            encoding="utf-8",
        )

        result = self.run_batch("-s", str(batch))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls.read_text(encoding="utf-8").splitlines()), 2)

    def test_invalid_input_starts_nothing(self):
        valid = self.write_spec("valid.json", "owner/valid")
        invalid = self.root / "invalid.json"
        invalid.write_text(
            json.dumps({"schema_version": 1, "taskset": ""}), encoding="utf-8"
        )

        result = self.run_batch("--spec", str(valid), str(invalid))

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid FleetSpec", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.artifact_root.exists())

    def test_one_failure_does_not_prevent_other_launches(self):
        failed = self.write_spec("failed.json", "fail/taskset")
        passed = self.write_spec("passed.json", "owner/passed")

        result = self.run_batch("--spec", str(failed), str(passed))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.calls.read_text(encoding="utf-8").splitlines()), 2)
        self.assertIn("fail/taskset", result.stderr)
        self.assertIn("FAILED(7)", result.stderr)
        self.assertIn("owner/passed", result.stderr)
        self.assertIn(" OK ", result.stderr)

    def test_multiple_openclaw_specs_are_rejected_before_launch(self):
        first = self.write_spec("pinchbench.json", "pinchbench")
        second = self.write_spec("clawbio.json", "clawbio")

        result = self.run_batch("--spec", str(first), str(second))

        self.assertEqual(result.returncode, 2)
        self.assertIn("at most one OpenClaw run", result.stderr)
        self.assertFalse(self.artifact_root.exists())
        self.assertFalse(self.calls.exists())

    def test_one_openclaw_and_one_harbor_spec_are_allowed(self):
        openclaw = self.write_spec("pinchbench.json", "pinchbench")
        harbor = self.write_spec("harbor.json", "owner/harbor")

        result = self.run_batch("--spec", str(openclaw), str(harbor))

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("DATASET_NAME=owner/harbor", calls[0])
        self.assertIn("FLEET_BATCH_HARBOR_RUNS=1", calls[0])

    def test_rollout_rejects_multiple_harbor_batch_members_before_startup(self):
        guard_root = self.root / "harbor-guard"
        guard_start = guard_root / "start.sh"
        guard_root.mkdir()
        guard_start.write_text(HARBOR_START.read_text(encoding="utf-8"), encoding="utf-8")
        (guard_root / "env.sh").write_text(
            'ROLLOUT="${ROLLOUT:-0}"\nRL_PORT="${RL_PORT:-19001}"\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({"FLEET_BATCH_HARBOR_RUNS": "2", "ROLLOUT": "1"})

        result = subprocess.run(
            ["bash", str(guard_start), "--detach"],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("only one Harbor run per Batch", result.stderr)
        self.assertIn("RL_PORT=19001", result.stderr)

    def test_harbor_children_clear_inherited_run_state_paths(self):
        first = self.write_spec("first.json", "owner/first")
        second = self.write_spec("second.json", "owner/second")
        shared_paths = {
            name: f"/shared/{name.lower()}" for name in HARBOR_RUN_STATE_VARS
        }

        result = self.run_batch(
            "--spec", str(first), str(second), env_overrides=shared_paths
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        for line in calls:
            for name in HARBOR_RUN_STATE_VARS[:6]:
                self.assertIn(f"{name}=<>", line)

    def assert_signal_is_forwarded_and_child_reaped(self, signal_number, exit_code):
        spec = self.write_spec("pinchbench.json", "pinchbench")
        child_pid_file = self.root / "child.pid"
        env = self.batch_env()
        env["STUB_CHILD_PID_FILE"] = str(child_pid_file)
        process = subprocess.Popen(
            [str(SCRIPT), "--spec", str(spec)],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not child_pid_file.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(child_pid_file.exists(), "foreground child did not start")
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))

            process.send_signal(signal_number)
            _, stderr = process.communicate(timeout=5)

            self.assertEqual(process.returncode, exit_code, stderr)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"foreground child {child_pid} was not reaped")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_term_is_forwarded_to_foreground_child_and_reaped(self):
        self.assert_signal_is_forwarded_and_child_reaped(signal.SIGTERM, 143)

    def test_int_is_forwarded_to_foreground_child_and_reaped(self):
        self.assert_signal_is_forwarded_and_child_reaped(signal.SIGINT, 130)

    def test_term_reaches_foreground_grandchildren(self):
        # The real ClawBio launcher does its work in grandchildren; killing
        # only the launcher PID orphans them. The batch signals the whole
        # process group, so the grandchild must die with the launcher.
        spec = self.write_spec("clawbio.json", "clawbio")
        grand_pid_file = self.root / "grandchild.pid"
        env = self.batch_env()
        env["STUB_GRANDCHILD_PID_FILE"] = str(grand_pid_file)
        process = subprocess.Popen(
            [str(SCRIPT), "--spec", str(spec)],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        grand_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not grand_pid_file.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(grand_pid_file.exists(), "grandchild did not start")
            grand_pid = int(grand_pid_file.read_text(encoding="utf-8"))

            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)

            self.assertEqual(process.returncode, 143, stderr)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(grand_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"foreground grandchild {grand_pid} survived cancellation")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if grand_pid is not None:
                try:
                    os.kill(grand_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_dry_run_prints_commands_without_starting_runner(self):
        first = self.write_spec("first.json", "owner/first")
        second = self.write_spec("second.json", "owner/second")

        result = self.run_batch("--spec", str(first), str(second), "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertEqual(result.stdout.count("Command: env"), 2)
        self.assertIn("DATASET_NAME=owner/first", result.stdout)
        self.assertIn("DATASET_NAME=owner/second", result.stdout)
        self.assertEqual(list(self.artifact_dir().glob("*.log")), [])

    def test_registry_taskset_produces_safe_short_run_id(self):
        spec = self.write_spec(
            "registry.json", "publisher/benchmark@2026/very-long-taskset-name"
        )

        result = self.run_batch("--spec", str(spec))

        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = re.search(
            r"RUN_ID=([^ ]+)", self.calls.read_text(encoding="utf-8")
        ).group(1)
        self.assertRegex(run_id, r"^[A-Za-z0-9_-]+$")
        self.assertLessEqual(len(run_id), 48)


if __name__ == "__main__":
    unittest.main()
