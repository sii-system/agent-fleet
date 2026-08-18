"""Read Harbor task results and monitor state for a verification run."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from harbor_monitor.artifacts import (
    HarborJobSnapshot,
    TaskInput,
    load_harbor_job_snapshot,
    load_manifest,
    load_task_file_manifest,
    load_task_records,
)
from harbor_monitor.classification import classify_task_status
from harbor_monitor.runner import run_loop

from ..artifact_io import read_json


def locate_queue_files(run_dir: Path, agent: str) -> tuple[Path, Path]:
    queue_dir = run_dir / "queue" / agent
    return queue_dir / "done.txt", queue_dir / "failed.txt"


def locate_native_runtime_files(run_dir: Path, agent: str) -> tuple[Path, Path, Path]:
    runtime_dir = run_dir / "runtime" / agent
    return (
        runtime_dir / "harbor-job-dir",
        runtime_dir / "harbor-benchmark.pid",
        runtime_dir / "harbor-benchmark.exit",
    )


def load_task_manifest(run_dir: Path) -> dict[str, str]:
    return load_manifest(run_dir / "task-manifest.tsv") or load_task_file_manifest(
        run_dir / "tasks.txt"
    )


def _load_native_snapshot(job_dir_file: Path) -> HarborJobSnapshot | None:
    try:
        raw_job_dir = job_dir_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return load_harbor_job_snapshot(Path(raw_job_dir)) if raw_job_dir else None


def _result_mtime(task: TaskInput) -> int:
    try:
        return Path(task.result_path).stat().st_mtime_ns if task.result_path else 0
    except OSError:
        return 0


def load_native_task_records(
    run_dir: Path, agent: str, manifest: dict[str, str]
) -> dict[str, TaskInput] | None:
    job_dir_file, _, _ = locate_native_runtime_files(run_dir, agent)
    snapshot = _load_native_snapshot(job_dir_file)
    if snapshot is None:
        return None
    if not manifest:
        return snapshot.tasks

    manifest_name_counts: dict[str, int] = {}
    for name in manifest.values():
        manifest_name_counts[name] = manifest_name_counts.get(name, 0) + 1

    tasks: dict[str, TaskInput] = {}
    consumed: set[str] = set()
    for index, name in manifest.items():
        matches = [
            (trial_name, task)
            for trial_name, task in snapshot.tasks.items()
            if task.task_name == name or task.task_name == f"terminal-bench/{name}"
        ]
        if manifest_name_counts[name] == 1 and matches:
            trial_name, task = max(
                matches,
                key=lambda match: (_result_mtime(match[1]), match[0]),
            )
            task.task_index = index
            task.task_name = name
            tasks[index] = task
            consumed.update(match[0] for match in matches)
        else:
            tasks[index] = TaskInput(task_index=index, task_name=name)
    for trial_name, task in snapshot.tasks.items():
        if trial_name not in consumed:
            tasks.setdefault(trial_name, task)
    return tasks


def collect_task_results(
    run_dir: Path, agent: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    done_path, failed_path = locate_queue_files(run_dir, agent)
    manifest = load_task_manifest(run_dir)
    tasks = load_native_task_records(run_dir, agent, manifest)
    if tasks is None:
        tasks = load_task_records(done_path, failed_path)
    for index, name in manifest.items():
        tasks.setdefault(index, TaskInput(task_index=index, task_name=name))

    records: dict[str, dict[str, Any]] = {}
    counts = {
        "total": len(tasks),
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }
    for index, task in sorted(tasks.items()):
        status, signals, evidence = classify_task_status(
            task, [run_dir, done_path.parent]
        )
        counts[status] += 1
        counts["finished"] += status != "not_complete"
        records[index] = {
            "task_index": index,
            "task_name": task.task_name,
            "task_complete_status": status,
            "task_result_signals": sorted(set(signals)),
            "evidence": evidence,
            "result_path": task.result_path or "",
        }
    if counts["finished"]:
        counts["success_rate"] = counts["complete_success"] / counts["finished"] * 100.0
    return records, counts


def read_monitor_snapshot(run_dir: Path) -> tuple[dict[str, Any] | None, str]:
    path = run_dir / "monitor" / "monitor-latest.json"
    try:
        if path.is_file():
            return read_json(path), str(path)
    except (OSError, ValueError):
        pass
    return None, ""


def generate_monitor_snapshot(
    run_dir: Path,
    output_dir: Path,
    agent: str,
    *,
    startup_grace: int = 0,
) -> tuple[dict[str, Any] | None, str]:
    done_path, failed_path = locate_queue_files(run_dir, agent)
    job_dir_file, pid_file, exit_file = locate_native_runtime_files(run_dir, agent)
    native_runtime = job_dir_file.is_file()
    monitor_dir = output_dir / "verification-monitor"
    monitor_output = monitor_dir / "monitor-latest.json"
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            run_loop(
                run_dir=run_dir,
                done_path=done_path,
                failed_path=failed_path,
                queue_dir=done_path.parent if done_path.parent.exists() else None,
                task_manifest_path=run_dir / "task-manifest.tsv",
                task_file_path=run_dir / "tasks.txt",
                restart_cmd=None,
                stop_cmd=None,
                output_path=monitor_output,
                poll_interval=1,
                max_retries=0,
                S_default=1800,
                S_min=900,
                S_max=3600,
                startup_grace=startup_grace,
                configured_timeout=None,
                total_override=None,
                running_override=None,
                claimed_override=None,
                remaining_override=None,
                user_report_output=monitor_dir / "user-notify-latest.json",
                analyzer_handover_output=monitor_dir / "analyzer-handover-latest.json",
                runner_action_output=monitor_dir / "runner-action-latest.json",
                loop_once=True,
                include_unknown_not_complete=True,
                state_path=monitor_dir / "monitor-state.json",
                harbor_job_dir_file=job_dir_file if native_runtime else None,
                harbor_pid_file=pid_file if native_runtime else None,
                harbor_exit_file=exit_file if native_runtime else None,
            )
    except (OSError, ValueError):
        return None, ""
    return (
        (read_json(monitor_output), str(monitor_output))
        if monitor_output.is_file()
        else (None, "")
    )
