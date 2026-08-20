"""Launch and inspect a Harbor verification rerun."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..validation import ValidationError, task_key
from .run_state import generate_monitor_snapshot
from .selection import sort_task_index

RUN_SCOPED_ENV_VARS = {
    "RUN_ID",
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
    "HARBOR_MONITOR_RESTART_CMD",
    "HARBOR_MONITOR_STOP_CMD",
    "HARBOR_ANALYZER_OUTPUT_DIR",
    "HARBOR_ANALYZER_PID_FILE",
    "HARBOR_ANALYZER_SUPERVISOR_PID_FILE",
    "HARBOR_ANALYZER_SUPERVISOR_ID_FILE",
    "HARBOR_ANALYZER_LOG_FILE",
    "HARBOR_ZELLIJ_SESSION_NAME",
    "HARBOR_QUEUE_WORKER",
    "RL_ZELLIJ_SESSION_NAME",
    "ZELLIJ_SESSION_NAME",
    "ZELLIJ",
    "ZELLIJ_PANE_ID",
    "FLEET_TASKS",
    "INCLUDE_TASKS",
    "HARBOR_INCLUDE_TASKS",
    "HARBOR_AGENT_IMPORT_PATH",
    "HARBOR_TASK_ID",
    "HARBOR_ANTHROPIC_MODEL",
    "HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL",
    "HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL",
    "HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "HARBOR_CLAUDE_CODE_SUBAGENT_MODEL",
    "NEXT_INDEX_FILE",
    "LOCK_FILE",
    "WORKERS_READY_FILE",
    "WORKERS_FAILED_FILE",
    "RL_TRACE_LOG",
    "RL_SERVER_LOG",
    "RL_SERVER_PID_FILE",
    "RL_QUEUE_DIR",
    "RL_ACTIVE_DIR",
    "RL_JOB_QUEUE_ROOT",
    "RL_JOB_RUNTIME_ROOT",
}

TERMINATION_GRACE_SECONDS = 5.0

GENERATED_MONITOR_FILES = {
    "monitor-state.json",
    "monitor-latest.json",
    "user-notify-latest.json",
    "analyzer-handover-latest.json",
    "runner-action-latest.json",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_log_tail(stream: Any) -> str:
    end = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, end - 16384))
    return stream.read().decode(errors="replace")[-4000:]


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_verifier_backups(run_dir: Path, agent: str) -> None:
    runtime_dir = run_dir / "runtime" / agent
    for path in runtime_dir.glob("verifier-uv.*"):
        shutil.rmtree(path, ignore_errors=True)


def monitor_is_terminal(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("benchmark_status") == "completed" or snapshot.get(
        "monitor_follow_decision"
    ) in {"stop_completed", "stop_action_required"}


def wait_for_monitor(
    run_dir: Path,
    output_dir: Path,
    agent: str,
    *,
    timeout_seconds: int,
    poll_interval: float,
) -> tuple[dict[str, Any] | None, str, bool]:
    monitor_dir = output_dir / "verification-monitor"
    for name in GENERATED_MONITOR_FILES:
        (monitor_dir / name).unlink(missing_ok=True)

    deadline = time.monotonic() + timeout_seconds
    latest: tuple[dict[str, Any] | None, str] = (None, "")
    while time.monotonic() < deadline:
        latest = generate_monitor_snapshot(run_dir, output_dir, agent, startup_grace=1)
        if latest[0] is not None and monitor_is_terminal(latest[0]):
            return *latest, False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.1, poll_interval), remaining))
    return *latest, True


def run_command(
    command: str | None,
    run_dir: Path,
    agent: str,
    *,
    task_source_path: str,
    selection_path: str,
    should_run: bool,
    timeout_seconds: float,
    dataset_name: str = "",
    dataset_path: str = "",
    model: str = "",
) -> dict[str, Any]:
    skipped_reason = "" if should_run else "no_sampled_tasks"
    if not command or not should_run:
        return {
            "command": command or "",
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "stdout_summary": "",
            "stderr_summary": "",
            "skipped_reason": skipped_reason,
        }
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValidationError(f"--rerun-command is invalid: {exc}") from None
    if not argv:
        raise ValidationError("--rerun-command must not be blank")
    try:
        run_dir = run_dir.resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        task_source = Path(task_source_path).resolve()
        task_text = task_source.read_text(encoding="utf-8")
        task_file = run_dir / "tasks.txt"
        task_file.write_text(task_text, encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot prepare verification rerun: {exc}") from None
    smoke_tasks = ",".join(
        task.strip() for task in task_text.splitlines() if task.strip()
    )
    inherited_queue_worker = os.environ.get("HARBOR_QUEUE_WORKER") == "1"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in RUN_SCOPED_ENV_VARS
    }
    if inherited_queue_worker:
        # Queue workers force one Harbor task per process. Verification runs a
        # complete smoke set directly, so env.sh must restore normal concurrency.
        env.pop("HARBOR_N_CONCURRENT", None)
    env.update(
        {
            "AGENT": agent,
            "HARBOR_AGENT_IMPORT_PATH": "",
            "TASK_SOURCE_FILE": str(task_source),
            "TASK_FILE": str(task_file),
            "FLEET_TASKS": smoke_tasks,
            "INCLUDE_TASKS": smoke_tasks,
            "HARBOR_INCLUDE_TASKS": smoke_tasks,
            "HARBOR_LIMIT": "",
            "HARBOR_RUNS": "1",
            "N_ATTEMPTS": "1",
            "MIN_TEST": "0",
            "OUTPUT_PATH": str(run_dir),
            "RESET_RUN": "1",
            "ROLLOUT": "0",
            "HARBOR_ONLINE_ANALYSIS": "0",
            "HARBOR_EARLY_STOP": "0",
            "HARBOR_MONITOR_ENABLED": "0",
            "HARBOR_ANALYZER_ENABLED": "0",
            "HARBOR_QUEUE_WORKER": "0",
            "HARBOR_FIXER_VERIFICATION_RERUN": "1",
            "HARBOR_FIXER_SMOKE_SELECTION": str(Path(selection_path).resolve()),
        }
    )
    if dataset_name:
        env["DATASET_NAME"] = dataset_name
    if dataset_path:
        env["DATASET_PATH"] = dataset_path
    if model:
        env["HARBOR_MODEL"] = model
    started_at = _utc_now()
    started = time.monotonic()
    timed_out = False
    try:
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            process = subprocess.Popen(
                argv,
                cwd=run_dir,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                _cleanup_verifier_backups(run_dir, agent)
                exit_code = 124
                timed_out = True
            stdout_summary = _read_log_tail(stdout_file)
            stderr_summary = _read_log_tail(stderr_file)
    except OSError as exc:
        raise ValidationError(f"cannot launch verification rerun: {exc}") from None
    if timed_out:
        stderr_summary = (
            stderr_summary
            + f"\nverification rerun timed out after {timeout_seconds:g} seconds"
        )[-4000:]
    return {
        "command": command,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "skipped_reason": "",
    }


def map_run_records(
    records: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> tuple[
    dict[tuple[str, str, str], dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    selected = {str(task["smoke_task_index"]): task for task in selection["tasks"]}
    errors: list[dict[str, Any]] = []
    if set(records) != set(selected):
        errors.append(
            {
                "error": "smoke_task_index_set_mismatch",
                "expected": sorted(selected, key=sort_task_index),
                "actual": sorted(records, key=sort_task_index),
            }
        )

    mapped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for smoke_index, task in selected.items():
        identity = {
            "task_index": task["original_task_index"],
            "task_name": task["task_name"],
            "attempt_id": task["attempt_id"],
        }
        record = records.get(smoke_index)
        if record is None:
            record = {
                "task_index": identity["task_index"],
                "task_name": identity["task_name"],
                "task_complete_status": "complete_unknown",
                "task_result_signals": ["result_missing"],
                "evidence": {},
                "result_path": "",
            }
        elif record["task_name"] != identity["task_name"]:
            errors.append(
                {
                    "error": "smoke_task_name_mismatch",
                    "smoke_task_index": smoke_index,
                    "expected_task_name": identity["task_name"],
                    "actual_task_name": record["task_name"],
                }
            )
        mapped[task_key(identity)] = {
            **record,
            "task_index": identity["task_index"],
            "task_name": identity["task_name"],
            "smoke_task_index": smoke_index,
        }

    unexpected = [
        records[index]
        for index in sorted(set(records) - set(selected), key=sort_task_index)
    ]
    return mapped, unexpected, errors
