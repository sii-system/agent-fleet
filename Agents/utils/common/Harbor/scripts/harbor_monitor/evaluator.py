"""Compose one monitor evaluation from artifact and runtime evidence."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .artifacts import TaskInput, load_task_records, parse_environment_events
from .classification import (
    classify_benchmark_status,
    classify_task_status,
    merge_task_runtime_evidence,
)


def now_ts() -> float:
    return time.time()


def evaluate_once(
    run_dir: Path,
    done_path: Path,
    failed_path: Path,
    tasks_manifest: dict[str, str],
    total: int | None,
    claimed: int | None,
    remaining: int | None,
    running: int,
    environment_events_raw: str | None,
    S: int,
    startup_grace: int,
    configured_timeout: int | None,
    max_retries: int,
    state: dict[str, Any],
    include_unknown_not_complete: bool,
    task_records: dict[str, TaskInput] | None = None,
    terminal_artifacts_missing: bool | None = None,
    run_finalized: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    environment_events = parse_environment_events(environment_events_raw)
    queue_files_missing = (
        not done_path.exists() or not failed_path.exists()
        if terminal_artifacts_missing is None
        else terminal_artifacts_missing
    )
    tasks = dict(task_records) if task_records is not None else load_task_records(done_path, failed_path)
    # Add manifest tasks for not_complete inference
    for task_index, task_name in tasks_manifest.items():
        if task_index in tasks:
            continue
        tasks[task_index] = TaskInput(task_index=task_index, task_name=task_name)
    if not tasks_manifest and total is not None:
        missing_tasks = max(0, total - len(tasks)) if task_records is not None else total
        for index in range(1, missing_tasks + 1):
            task_index = f"pending-{index}" if task_records is not None else str(index)
            if task_index not in tasks:
                tasks[task_index] = TaskInput(task_index=task_index)

    done_count = sum(1 for task in tasks.values() if task.in_done)
    failed_count = sum(1 for task in tasks.values() if task.in_failed)
    finished = sum(1 for task in tasks.values() if task.in_done or task.in_failed)

    if total is None:
        total = len(tasks_manifest) if tasks_manifest else None
    if claimed is None and total is not None:
        claimed = total - (remaining if remaining is not None else 0)

    now = now_ts()
    unclaimed_remaining = remaining
    if unclaimed_remaining is None and total is not None and claimed is not None:
        unclaimed_remaining = max(0, total - claimed)
    unfinished = max(0, total - finished) if total is not None else None
    previous = state.get("history", [])
    history: list[dict[str, Any]] = [item for item in previous if isinstance(item, dict)]

    prev_sample = history[-1] if history else None
    prev_finished = prev_sample.get("finished") if prev_sample else None
    last_progress_ts = state.get("last_progress_ts")
    if prev_finished is None:
        finished_delta = 0
        if prev_sample is not None and last_progress_ts is None:
            last_progress_ts = float(prev_sample.get("ts", now))
        stalled_window = (
            prev_sample is not None
            and running > 0
            and unfinished is not None
            and unfinished > 0
            and last_progress_ts is not None
            and (now - float(last_progress_ts) >= S)
            and all(h.get("running", 0) > 0 for h in history[-3:] if isinstance(h, dict))
        )
    else:
        finished_delta = max(0, finished - int(prev_finished))
        if finished_delta > 0:
            last_progress_ts = now
        elif last_progress_ts is None:
            last_progress_ts = float(history[-1].get("ts", now))
        stalled_window = (
            running > 0
            and unfinished is not None
            and unfinished > 0
            and last_progress_ts is not None
            and (now - float(last_progress_ts) >= S)
            and all(h.get("running", 0) > 0 for h in history[-3:] if isinstance(h, dict))
        )
    if prev_sample is None:
        last_progress_ts = now

    if finished_delta > 0:
        last_progress_ts = now

    history.append(
        {
            "ts": now,
            "finished": finished,
            "running": running,
            "remaining": unclaimed_remaining,
            "unfinished": unfinished,
            "status": "",
        }
    )
    history = history[-120:]

    finished_deltas: list[int] = []
    for i in range(1, len(history)):
        if "finished" not in history[i - 1] or "finished" not in history[i]:
            finished_deltas.append(0)
            continue
        prev_v = int(history[i - 1].get("finished", 0))
        cur_v = int(history[i].get("finished", 0))
        finished_deltas.append(max(0, cur_v - prev_v))

    raw_run_start_ts = state.get("run_start_ts")
    if raw_run_start_ts is None and previous and isinstance(previous[0], dict):
        raw_run_start_ts = previous[0].get("ts")
    try:
        run_start_ts = float(raw_run_start_ts) if raw_run_start_ts is not None else now
    except (TypeError, ValueError):
        run_start_ts = now
    elapsed_since_run_start = max(0.0, now - run_start_ts)
    startup_grace_active = (
        running == 0
        and (unfinished or 0) > 0
        and finished == 0
        and elapsed_since_run_start < startup_grace
    )
    timeout_reached = configured_timeout is not None and (unfinished or 0) > 0 and elapsed_since_run_start >= configured_timeout

    if startup_grace_active:
        benchmark_status, reason = "running", "starting"
    elif queue_files_missing and total is not None and not ((unfinished or 0) == 0 and running == 0):
        benchmark_status, reason = "blocked", "unknown_or_conflicting_fields"
    else:
        benchmark_status, reason = classify_benchmark_status(
            total=total,
            claimed=claimed,
            remaining=unclaimed_remaining,
            running=running,
            finished_count=finished,
            finished_deltas=finished_deltas,
            environment_events=environment_events,
            stalled_candidate=stalled_window,
            run_finalized=run_finalized,
        )
        if (
            benchmark_status == "blocked"
            and reason == "abnormal_exit"
            and state.get("retry_count", 0) > 0
            and elapsed_since_run_start < startup_grace
        ):
            benchmark_status, reason = "running", "recovering"
        elif timeout_reached and running > 0:
            # A live Harbor worker may legitimately run longer than the monitor SLA.
            # Report the overrun, but do not infer that the task is safe to restart.
            benchmark_status, reason = "running", "timeout_reached"

    task_summary = {
        "not_complete": 0,
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "total_evaluated": len(tasks),
    }
    task_handover: list[dict[str, Any]] = []
    no_complete_progress = running == 0 and finished_delta == 0
    blocked_duration = (now - float(last_progress_ts)) if last_progress_ts is not None else None
    for task in tasks.values():
        status, signals, evidence = classify_task_status(task, [run_dir, done_path.parent])
        task_summary[status] += 1
        need_send = status in {"complete_failed", "complete_unknown"}
        if status == "not_complete" and include_unknown_not_complete and (
            not startup_grace_active
            and (
                (no_complete_progress and running == 0)
                or (benchmark_status == "blocked" and reason == "abnormal_exit")
            )
        ):
            need_send = True
            signals.append("not_complete_with_no_progress")
        if need_send:
            task_handover.append(
                {
                    "task_index": task.task_index,
                    "task_name": task.task_name,
                    "result_path": task.result_path,
                    "task_complete_status": status,
                    "task_result_signals": sorted(set(signals)),
                    "evidence": merge_task_runtime_evidence(
                        evidence,
                        elapsed_since_run_start=elapsed_since_run_start,
                        blocked_duration=blocked_duration,
                        S=S,
                        configured_timeout=configured_timeout,
                    ),
                }
            )
        if status == "not_complete":
            continue

    action_type = "wait"
    if benchmark_status == "blocked" and reason == "abnormal_exit":
        if state.get("retry_count", 0) >= max_retries:
            action_type = "notify"
        else:
            action_type = "restart"
    elif benchmark_status == "running" and reason == "timeout_reached":
        action_type = "notify"
    elif benchmark_status == "completed":
        action_type = "stop"
    elif benchmark_status == "blocked":
        action_type = "notify"

    return_data = {
        "benchmark_status": benchmark_status,
        "status_reason": reason,
        "finished": finished,
        "finished_delta": finished_delta,
        "finished_deltas": finished_deltas,
        "remaining": unclaimed_remaining,
        "unclaimed_remaining": unclaimed_remaining,
        "unfinished": unfinished,
        "running": running,
        "stalled_duration_reached": reason == "suspected_stalled" and stalled_window,
        "evidence": {
            "running_workers": running,
            "remaining": unclaimed_remaining,
            "unfinished": unfinished,
            "finished_delta": finished_delta,
            "blocked_duration": blocked_duration,
            "startup_grace": startup_grace,
            "startup_grace_active": startup_grace_active,
            "configured_timeout": configured_timeout,
            "elapsed_since_run_start": elapsed_since_run_start,
            "run_start_ts": run_start_ts,
            "environment_events": environment_events,
        },
        "loop_id": now,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "task_summary": task_summary,
        "task_handover": task_handover,
        "task_summary_action": {
            "total": total,
            "done": done_count,
            "failed": failed_count,
            "claimed": claimed,
            "unclaimed_remaining": unclaimed_remaining,
            "unfinished": unfinished,
            "queue_files_missing": queue_files_missing,
            "environment_events": environment_events,
        },
    }
    action = {
        "type": action_type,
        "retry_count": state.get("retry_count", 0),
        "reason": reason,
        "external_control_performed": False,
        "compatibility_note": "Harbor monitor control is command-based and runner-neutral; no runner-specific control is performed.",
    }
    if action_type == "notify" and benchmark_status == "blocked" and reason == "abnormal_exit":
        action["reason"] = f"restart_retries_exhausted(max_retries={max_retries})"
    return return_data, action, history, {"last_progress_ts": last_progress_ts, "run_start_ts": run_start_ts}
