"""Build the monitor's user, analyzer, and runner output contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .classification import SIGNAL_DEFINITIONS

RUNTIME_EVIDENCE_KEYS = {
    "elapsed_since_run_start",
    "blocked_duration",
    "S",
    "configured_timeout",
}


def build_notify_incident_key(output: dict[str, Any], action: dict[str, Any]) -> str:
    evidence = output.get("evidence") if isinstance(output.get("evidence"), dict) else {}
    key = {
        "status_reason": output.get("status_reason"),
        "retry_count": action.get("retry_count"),
        "finished": output.get("finished"),
        "unfinished": output.get("unfinished"),
        "running": output.get("running"),
        "run_start_ts": evidence.get("run_start_ts"),
    }
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def suggested_user_checks(benchmark_status: str, status_reason: str, action: dict[str, Any]) -> list[str]:
    checks: list[str] = []
    action_type = str(action.get("type") or "wait")
    if status_reason == "timeout_reached":
        checks.append("Review the configured monitoring SLA and current Harbor worker state before intervening.")
    elif status_reason == "abnormal_exit":
        checks.extend(
            [
                "Check launcher/opik-harbor logs for exit codes and recent errors.",
                "Check the Docker daemon, disk, API quota, and network status.",
            ]
        )
    elif status_reason == "degraded":
        checks.append("Check online-analysis/environment-summary.json for new environment alerts.")
    elif status_reason == "unknown_or_conflicting_fields":
        checks.extend(
            [
                "Check that run_dir, queue_dir, the task manifest, and total/claimed/running fields are readable.",
                "Check whether Harbor initialized the queue files.",
            ]
        )
    if action_type == "notify":
        checks.append("Review Harbor queue, worker, Docker/API, and disk evidence before handling the benchmark.")
    if action.get("control_exit_code") not in (None, 0) or action.get("control_error"):
        checks.append("The Harbor control command failed; inspect control_stdout/control_error and handle it manually.")
    return checks


def build_user_notify(
    output: dict[str, Any],
    action: dict[str, Any],
    max_retries: int,
    run_dir: Path,
    queue_dir: Path | None,
    output_path: Path | None,
) -> dict[str, Any]:
    benchmark_status = str(output.get("benchmark_status") or "blocked")
    status_reason = str(output.get("status_reason") or "")
    action_type = str(action.get("type") or "wait")
    task_summary = output.get("task_summary") if isinstance(output.get("task_summary"), dict) else {}
    retry_count = int(action.get("retry_count") or 0)

    control_failed = action.get("control_exit_code") not in (None, 0) or bool(action.get("control_error"))
    required = action_type in {"restart", "stop", "notify"} or status_reason in {"degraded", "unknown_or_conflicting_fields"} or control_failed
    if action_type == "notify" or control_failed:
        severity = "action_required"
    elif action_type == "restart" or status_reason in {"abnormal_exit", "timeout_reached", "degraded", "unknown_or_conflicting_fields"}:
        severity = "warning"
    else:
        severity = "info"

    message_parts = [
        f"benchmark_status={benchmark_status}",
        f"status_reason={status_reason}",
        f"monitor_action={action_type}",
        f"retry_count={retry_count}/{max_retries}",
    ]
    if action.get("control_exit_code") is not None:
        message_parts.append(f"control_exit_code={action.get('control_exit_code')}")
    message = "; ".join(message_parts)

    if action_type == "notify" or control_failed or status_reason == "unknown_or_conflicting_fields":
        human_action_needed = "Human review is required for the Harbor queue, worker, Docker/API, and disk evidence."
    elif action_type == "restart":
        human_action_needed = "No human action is currently required; the monitor executed the Harbor restart command."
    elif action_type == "stop":
        human_action_needed = "No human action is required; the monitor finalized the completed Harbor run."
    else:
        human_action_needed = "No human action is required."

    return {
        "audience": "user",
        "kind": "notify_report",
        "required": required,
        "severity": severity,
        "message": message,
        "human_action_needed": human_action_needed,
        "suggested_checks": suggested_user_checks(benchmark_status, status_reason, action),
        "benchmark_status": benchmark_status,
        "status_reason": status_reason,
        "monitor_action_type": action_type,
        "runner_action_type": action_type,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "task_summary": task_summary,
        "evidence": output.get("evidence", {}),
        "paths": {
            "run_dir": str(run_dir),
            "queue_dir": str(queue_dir) if queue_dir else None,
            "monitor_output": str(output_path) if output_path else None,
        },
    }


def _analyzer_handover_id(*, run_id: str | None, tasks: list[dict[str, Any]]) -> str:
    handover_key = {
        "schema_version": 2,
        "run_id": run_id,
        "tasks": [task.get("terminal_fingerprint") for task in tasks],
    }
    handover_encoded = json.dumps(
        handover_key,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256-{hashlib.sha256(handover_encoded).hexdigest()}"


def build_analyzer_handover_for_tasks(
    handover: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a handover v2 payload for a task subset using the same identity rules."""

    payload = dict(handover)
    payload["tasks"] = [dict(task) for task in tasks]
    payload["should_run_analyzer"] = bool(tasks)
    payload["handover_id"] = _analyzer_handover_id(
        run_id=payload.get("run_id") if isinstance(payload.get("run_id"), str) else None,
        tasks=payload["tasks"],
    )
    return payload


def build_analyzer_handover(
    output: dict[str, Any],
    *,
    run_dir: Path | None = None,
    queue_dir: Path | None = None,
) -> dict[str, Any]:
    raw_tasks = output.get("task_handover") if isinstance(output.get("task_handover"), list) else []
    tasks: list[dict[str, Any]] = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            continue
        task = dict(raw_task)
        if not str(task.get("task_name") or "").strip():
            continue
        task.setdefault("attempt_id", None)
        evidence = task.get("evidence")
        stable_evidence = (
            {key: value for key, value in evidence.items() if key not in RUNTIME_EVIDENCE_KEYS}
            if isinstance(evidence, dict)
            else evidence
        )
        fingerprint_payload = {
            "task_index": task.get("task_index"),
            "task_name": task.get("task_name"),
            "attempt_id": task.get("attempt_id"),
            "result_path": task.get("result_path"),
            "task_complete_status": task.get("task_complete_status"),
            "task_result_signals": task.get("task_result_signals"),
            "evidence": stable_evidence,
        }
        encoded = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        task["terminal_fingerprint"] = f"sha256-{hashlib.sha256(encoded).hexdigest()}"
        tasks.append(task)
    should_run = len(tasks) > 0
    if should_run:
        instruction = (
            "Analyze only tasks listed in tasks. Classify root cause for complete_failed, "
            "complete_unknown, and eligible not_complete tasks. Use signal_definitions to interpret "
            "task_result_signals; they are evidence tags, not final env/model attribution. "
            "Do not re-analyze complete_success tasks."
        )
    else:
        instruction = (
            "No analyzer run is needed for this sample because no failed/unknown/not_complete tasks were handed over. "
            "If tasks are present in another sample, task_result_signals are evidence tags, not final env/model attribution."
        )
    run_id = run_dir.name if run_dir else None
    return {
        "schema_version": 2,
        "audience": "analyzer_subagent",
        "kind": "task_analysis_handover",
        "handover_id": _analyzer_handover_id(run_id=run_id, tasks=tasks),
        "generated_at": output.get("timestamp"),
        "run_id": run_id,
        "agent": queue_dir.name if queue_dir else None,
        "paths": {
            "run_dir": str(run_dir) if run_dir else None,
            "queue_dir": str(queue_dir) if queue_dir else None,
        },
        "should_run_analyzer": should_run,
        "instruction": instruction,
        "analyze_statuses": ["complete_failed", "complete_unknown", "not_complete"],
        "skip_statuses": ["complete_success"],
        "signal_definitions": SIGNAL_DEFINITIONS,
        "task_selection_policy": (
            "Monitor includes complete_failed and complete_unknown by default; it includes not_complete "
            "when the run has no active workers and no recent progress. Active tasks that only exceed "
            "the stall threshold or configured monitoring SLA remain with the monitor. "
            "Signals explain monitor evidence only; they are not final env/model attribution."
        ),
        "task_summary": output.get("task_summary", {}),
        "tasks": tasks,
    }


def build_runner_action(
    action: dict[str, Any],
    benchmark_status: str,
    status_reason: str,
    evidence: dict[str, Any],
    max_retries: int,
) -> dict[str, Any]:
    action_type = str(action.get("type") or "wait")
    control_type = str(action.get("control_type") or action_type)
    control_failed = action.get("control_exit_code") not in (None, 0) or bool(action.get("control_error"))
    control_attempted = bool(action.get("control_attempted"))
    control_performed = bool(action.get("external_control_performed"))
    retry_count = int(action.get("retry_count", 0) or 0)
    return {
        "audience": "harbor_runner_control",
        "kind": "harbor_control_action",
        "type": action_type,
        "should_execute": False,
        "already_executed_by_monitor": control_performed,
        "restart_attempted_by_monitor": control_type == "restart" and control_attempted,
        "stop_attempted_by_monitor": control_type == "stop" and control_attempted,
        "stop_auto_retry": action_type in {"stop", "notify"} or control_failed,
        "requires_human": action_type == "notify" or control_failed,
        "benchmark_status": benchmark_status,
        "status_reason": status_reason,
        "evidence": evidence,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "reason": action.get("reason", ""),
        "control_type": control_type if control_attempted else None,
        "control_exit_code": action.get("control_exit_code"),
        "control_error": action.get("control_error"),
        "restart_exit_code": action.get("control_exit_code") if control_type == "restart" else None,
        "restart_error": action.get("control_error") if control_type == "restart" else None,
        "external_control_performed": control_performed,
        "auto_retry_supported": action_type == "restart" and control_performed and retry_count < max_retries,
        "compatibility_note": "Harbor control is command-based and runner-neutral; monitor does not know or call runner-specific commands.",
        "contract": {
            "wait": "Harbor run is still observable; sample Harbor artifacts again after interval.",
            "restart": "Monitor executes only the configured run-local Harbor restart command with shell=False; recovery is confirmed by later samples.",
            "stop": "Monitor executes the configured run-local Harbor stop command when provided, then stops the follow loop.",
            "notify": "Harbor artifacts indicate blocked or ambiguous state that was not automatically recovered; surface user_notify and wait for human decision.",
        },
    }
