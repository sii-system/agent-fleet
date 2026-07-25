"""Classify task results and benchmark progress."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import TaskInput, read_result_json, to_float_value


def classify_task_status(
    task: TaskInput,
    result_base_dirs: list[Path],
    include_result_debug: bool = True,
) -> tuple[str, list[str], dict[str, Any]]:
    signals: list[str] = []
    task_result_json_ok, reward_json_from_result, is_resolved, result_exception = read_result_json(
        task.result_path,
        result_base_dirs,
    )
    reward_raw_float = to_float_value(task.reward_raw)
    reward_conflict = (
        reward_raw_float is not None
        and reward_json_from_result is not None
        and reward_raw_float != reward_json_from_result
    )

    if reward_raw_float is not None:
        reward_float = reward_raw_float
        reward_source = "reward_raw"
    else:
        reward_float = reward_json_from_result
        reward_source = "reward_json"

    exception = (task.exception_type or "").strip()
    if not exception:
        exception = (result_exception or "").strip()

    in_both = task.in_done and task.in_failed
    if in_both:
        signals.append("state_in_both_done_and_failed")
        signals.append("state_conflict")

    prefer_failed_record = task.in_failed and (not in_both or bool(task.early_stop_reason))
    prefer_done_record = task.in_done and (
        not task.in_failed or (in_both and not task.early_stop_reason and task_result_json_ok)
    )

    if prefer_failed_record:
        status = "complete_failed"
        rc = to_float_value(task.rc)
        if rc is not None:
            if int(rc) != 0:
                signals.append("failed_rc_nonzero")
            else:
                signals.append("failed_rc_zero")
        else:
            signals.append("failed_rc_missing_or_invalid")
        if task.early_stop_reason:
            signals.append("early_stop_reason_nonempty")
    elif prefer_done_record:
        if exception:
            signals.append("exception_type_nonempty")

        if is_resolved is False:
            status = "complete_failed"
            signals.append("is_resolved_false")
        elif reward_conflict:
            status = "complete_unknown"
            signals.append("reward_conflict")
        elif reward_float is None:
            if exception:
                status = "complete_failed"
            else:
                status = "complete_unknown"
                if task.result_path and not task_result_json_ok:
                    signals.append("result_missing")
        elif reward_float == 1.0:
            if task.result_path and not task_result_json_ok:
                status = "complete_unknown"
                signals.append("result_missing")
            else:
                status = "complete_success"
        elif reward_float == 0.0:
            status = "complete_failed"
            signals.append("reward_zero")
        else:
            status = "complete_unknown"
            signals.append("reward_unexpected")
    elif in_both:
        status = "complete_unknown"
    else:
        status = "not_complete"

    evidence = {
        "reward_raw": task.reward_raw,
        "exception_type": task.exception_type or "",
        "rc": task.rc,
        "early_stop_reason": task.early_stop_reason or "",
        "result_json": task_result_json_ok,
        "reward_json": reward_json_from_result,
        "is_resolved": is_resolved,
    }
    if include_result_debug:
        evidence["reward_source"] = reward_source

    if status in {"complete_failed", "complete_unknown"} and task.in_done and not prefer_failed_record:
        if not task.result_path and "result_path_missing" not in signals:
            signals.append("result_path_missing")
        if task.result_path and not task_result_json_ok and "result_missing" not in signals:
            signals.append("result_missing")
    return status, signals, evidence


def merge_task_runtime_evidence(
    evidence: dict[str, Any],
    *,
    elapsed_since_run_start: float,
    blocked_duration: float | None,
    S: int,
    configured_timeout: int | None,
) -> dict[str, Any]:
    enriched = dict(evidence)
    enriched.update(
        {
            "elapsed_since_run_start": elapsed_since_run_start,
            "blocked_duration": blocked_duration,
            "S": S,
            "configured_timeout": configured_timeout,
        }
    )
    return enriched


def classify_benchmark_status(
    total: int | None,
    claimed: int | None,
    remaining: int | None,
    running: int,
    finished_count: int,
    finished_deltas: list[int],
    environment_events: dict | list | None,
    stalled_candidate: bool,
    run_finalized: bool = True,
) -> tuple[str, str]:
    if total is None or running < 0:
        return "blocked", "unknown_or_conflicting_fields"
    finished = finished_count
    finished_delta = finished_deltas[-1] if finished_deltas else 0
    # Defensive: avoid unstable data.
    finished_delta = max(finished_delta, 0)
    unclaimed_remaining = remaining if remaining is not None else (total - claimed if claimed is not None else None)
    if unclaimed_remaining is not None:
        unclaimed_remaining = max(0, unclaimed_remaining)
    unfinished = max(0, total - finished)

    if unfinished == 0 and running == 0 and run_finalized:
        return "completed", "completed"
    if unfinished == 0 and running > 0:
        return "running", "finalizing"
    if unfinished == 0 and not run_finalized:
        return "blocked", "abnormal_exit"

    if total and claimed is not None and claimed <= 1 and running > 0 and finished == 0 and not stalled_candidate:
        return "running", "starting"

    if unfinished > 0 and running > 0:
        if finished_delta > 0:
            if has_environment_events(environment_events):
                return "running", "degraded"
            return "running", "progressing"
        if has_environment_events(environment_events):
            return "running", "degraded"
        if stalled_candidate:
            return "running", "suspected_stalled"
        return "running", "no_progress_under_threshold"

    if running == 0 and unfinished > 0:
        return "blocked", "abnormal_exit"
    return "blocked", "unknown_or_conflicting_fields"


def has_environment_events(events: dict[str, Any] | list[Any] | None) -> bool:
    if isinstance(events, list):
        return len(events) > 0
    if isinstance(events, dict):
        if "items" in events:
            items = events.get("items")
            if isinstance(items, list):
                return len(items) > 0
            return bool(items)
        by_type = events.get("monitor_environment_events_by_type")
        if isinstance(by_type, dict):
            return any(bool(value) for value in by_type.values())
        event_count = events.get("event_count")
        if isinstance(event_count, (int, float)):
            return event_count > 0
        if isinstance(event_count, str):
            parsed = to_float_value(event_count)
            if parsed is not None:
                return parsed > 0
        metadata_keys = {"schema", "generated_at", "run_dir", "event_count", "task_blocking_event_count"}
        return any(bool(value) for key, value in events.items() if key not in metadata_keys)
    return False
SIGNAL_DEFINITIONS: dict[str, str] = {
    "state_in_both_done_and_failed": "done.txt and failed.txt both contain this task_index; terminal queue records conflict.",
    "state_conflict": "Terminal queue records conflict; do not trust either side as a single-source result.",
    "failed_rc_nonzero": "failed.txt contains this task and rc is non-zero.",
    "failed_rc_zero": "failed.txt contains this task and rc is zero.",
    "failed_rc_missing_or_invalid": "failed.txt contains this task but rc is missing or cannot be parsed.",
    "early_stop_reason_nonempty": "failed.txt contains a non-empty early_stop_reason.",
    "exception_type_nonempty": "done/result exception_type is non-empty; it determines failure only when reward is unavailable.",
    "is_resolved_false": "result.json verifier_result.is_resolved is false.",
    "reward_zero": "done/result reward is 0.",
    "result_path_missing": "No result_path was recorded for this terminal record.",
    "result_missing": "result_path is missing, unreadable, or not parseable.",
    "reward_unexpected": "reward exists but is not an expected success/failure value.",
    "reward_conflict": "reward_raw and reward_json disagree.",
    "timeout_reached": "The run exceeded the configured monitoring SLA without a terminal queue record.",
    "not_complete_with_no_progress": "No terminal queue record exists and the run has no progress after the threshold.",
}
