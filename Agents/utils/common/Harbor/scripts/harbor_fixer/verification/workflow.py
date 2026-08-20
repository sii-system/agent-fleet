"""Deterministic smoke-verification workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor_monitor.runner import process_is_alive

from ..artifact_io import read_json, write_json, write_json_atomic
from ..validation import (
    HARBOR_AGENTS,
    TASK_VERIFICATION_STATUSES,
    ValidationError,
    json_sha256,
    task_key,
    validate_fix_plan_set,
    validate_verification_input,
    validate_verification_result,
)
from .outcomes import (
    aggregate_status,
    exec_failure_reason,
    plan_status,
    verification_status,
)
from .rerun import map_run_records, run_command, wait_for_monitor
from .run_state import (
    collect_task_results,
    generate_monitor_snapshot,
    locate_native_runtime_files,
    read_monitor_snapshot,
)
from .selection import build_smoke_selection, plan_exec_map, plan_tasks

DEFAULT_RERUN_TIMEOUT_SECONDS = 600


def _prepare_verification_output(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "verification-result-latest.json").unlink(missing_ok=True)
    except OSError as exc:
        raise ValidationError(f"cannot prepare verification output: {exc}") from None


def build_verification_input(
    fix_plan_path: Path,
    exec_result_path: Path,
    verification_run_dir: Path,
    *,
    rerun_command: str | None,
    monitor_policy: str,
    output_dir: Path,
    agent: str | None = None,
    rerun_timeout: int = DEFAULT_RERUN_TIMEOUT_SECONDS,
    monitor_wait_timeout: int = 3600,
    monitor_poll_interval: float = 30.0,
    verification_task_limit_per_plan: int = 2,
    dataset_name: str = "",
    dataset_path: str = "",
    model: str = "",
) -> dict[str, Any]:
    fix_plan_path = fix_plan_path.expanduser().resolve()
    exec_result_path = exec_result_path.expanduser().resolve()
    verification_run_dir = verification_run_dir.expanduser().resolve()
    fix_plan = read_json(fix_plan_path)
    exec_result = read_json(exec_result_path)
    validate_fix_plan_set(fix_plan)
    source_agent = str(fix_plan["source"].get("agent") or "")
    if source_agent and agent and source_agent != agent:
        raise ValidationError("--agent does not match fix plan source")
    agent = source_agent or agent
    if not agent:
        choices = ", ".join(sorted(HARBOR_AGENTS))
        raise ValidationError(
            f"fix plan does not identify an agent; pass --agent with one of: {choices}"
        )
    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_input",
        "agent": agent,
        "fix_plan_path": str(fix_plan_path),
        "exec_result_path": str(exec_result_path),
        "verification_run_dir": str(verification_run_dir),
        "output_dir": str(output_dir),
        "rerun_command": rerun_command or "",
        "rerun_timeout": rerun_timeout,
        "monitor_policy": monitor_policy,
        "monitor_wait_timeout": monitor_wait_timeout,
        "monitor_poll_interval": monitor_poll_interval,
        "verification_mode": "smoke_test",
        "verification_task_limit_per_plan": verification_task_limit_per_plan,
        "dataset_name": dataset_name,
        "dataset_path": dataset_path,
        "model": model,
        "fix_plan": fix_plan,
        "exec_result": exec_result,
    }
    validate_verification_input(payload)
    return payload


def run_verification(
    verification_input: dict[str, Any], output_dir: Path
) -> dict[str, Any]:
    _prepare_verification_output(output_dir)
    validate_verification_input(verification_input)
    write_json(output_dir / "verification-input.json", verification_input)

    fix_plan = verification_input["fix_plan"]
    exec_result = verification_input["exec_result"]
    agent = str(verification_input["agent"])
    exec_plans = plan_exec_map(exec_result)
    policy_status = str(exec_result["policy_status"])
    tasks_by_plan = plan_tasks(fix_plan)
    run_dir = Path(verification_input["verification_run_dir"])
    monitor_policy = verification_input["monitor_policy"]
    limit = int(verification_input["verification_task_limit_per_plan"])
    rerun_command = str(verification_input.get("rerun_command") or "")
    selection = build_smoke_selection(
        fix_plan, exec_plans, limit_per_plan=limit, output_dir=output_dir
    )
    selected = selection["tasks"]
    rerun = run_command(
        rerun_command or None,
        run_dir,
        agent,
        task_source_path=selection["source"]["task_source_path"],
        selection_path=selection["source"]["selection_path"],
        should_run=bool(selected),
        dataset_name=str(verification_input.get("dataset_name") or ""),
        dataset_path=str(verification_input.get("dataset_path") or ""),
        model=str(verification_input.get("model") or ""),
        timeout_seconds=int(
            verification_input.get(
                "rerun_timeout", DEFAULT_RERUN_TIMEOUT_SECONDS
            )
        ),
    )

    monitor, monitor_path = read_monitor_snapshot(run_dir)
    monitor_timed_out = False
    if selected and monitor_policy in {"auto", "on"}:
        _, benchmark_pid_file, _ = locate_native_runtime_files(run_dir, agent)
        should_wait_for_rerun = rerun["exit_code"] == 0 or process_is_alive(
            benchmark_pid_file
        )
        if should_wait_for_rerun:
            monitor, monitor_path, monitor_timed_out = wait_for_monitor(
                run_dir,
                output_dir,
                agent,
                timeout_seconds=int(verification_input.get("monitor_wait_timeout", 3600)),
                poll_interval=float(verification_input.get("monitor_poll_interval", 30.0)),
            )
        elif monitor is None:
            monitor, monitor_path = generate_monitor_snapshot(
                run_dir, output_dir, agent
            )

    empty_summary = {
        "total": 0,
        "complete_success": 0,
        "complete_failed": 0,
        "complete_unknown": 0,
        "not_complete": 0,
        "finished": 0,
        "success_rate": 0.0,
    }
    records, run_summary = (
        collect_task_results(run_dir, agent) if selected else ({}, empty_summary)
    )
    mapped, unexpected, mapping_errors = map_run_records(records, selection)
    sampled_keys = {
        task_key(
            {
                "task_index": task["original_task_index"],
                "task_name": task["task_name"],
                "attempt_id": task["attempt_id"],
            }
        )
        for task in selected
    }
    smoke_indexes = {
        task_key(
            {
                "task_index": task["original_task_index"],
                "task_name": task["task_name"],
                "attempt_id": task["attempt_id"],
            }
        ): task["smoke_task_index"]
        for task in selected
    }

    task_results: list[dict[str, Any]] = []
    plan_results: list[dict[str, Any]] = []
    for plan_id, tasks in tasks_by_plan.items():
        exec_status = str(exec_plans[plan_id]["status"])
        plan_task_results: list[dict[str, Any]] = []
        for task in tasks:
            key = task_key(task)
            record = mapped.get(key) if key in sampled_keys else None
            result = {
                "task": {
                    "task_index": str(task["task_index"]),
                    "task_name": str(task["task_name"]),
                    "attempt_id": task["attempt_id"],
                },
                "plan_id": plan_id,
                "sampled": key in sampled_keys,
                "smoke_task_index": smoke_indexes.get(key, ""),
                "exec_status": exec_status,
                "exec_failure_reason": exec_failure_reason(exec_status, policy_status),
                "new_run": record,
                "verification_status": verification_status(exec_status, record),
            }
            task_results.append(result)
            plan_task_results.append(result)

        sampled_indexes = [
            result["task"]["task_index"]
            for result in plan_task_results
            if result["sampled"]
        ]
        statuses = [result["verification_status"] for result in plan_task_results]
        all_indexes = [str(task["task_index"]) for task in tasks]
        unsampled_indexes = [
            result["task"]["task_index"]
            for result in plan_task_results
            if not result["sampled"]
        ]
        plan_results.append(
            {
                "plan_id": plan_id,
                "exec_status": exec_status,
                "exec_failure_reason": exec_failure_reason(exec_status, policy_status),
                "task_indexes": all_indexes,
                "sampled_task_indexes": sampled_indexes,
                "unsampled_task_indexes": unsampled_indexes,
                "sampled_task_count": len(sampled_indexes),
                "unsampled_task_count": len(unsampled_indexes),
                "status": plan_status(statuses),
                "verification_status_counts": {
                    status: statuses.count(status)
                    for status in sorted(TASK_VERIFICATION_STATUSES)
                },
            }
        )

    plan_task_count = sum(len(tasks) for tasks in tasks_by_plan.values())
    run_summary.update(
        {
            "scope": "smoke_sample",
            "verification_mode": "smoke_test",
            "sampled_task_count": len(selected),
            "plan_task_count": plan_task_count,
            "unsampled_task_count": plan_task_count - len(selected),
        }
    )
    status = aggregate_status(
        [result["verification_status"] for result in task_results],
        rerun["exit_code"],
    )
    if (
        mapping_errors
        or monitor_timed_out
        or (selected and monitor_policy == "on" and monitor is None)
    ):
        status = "inconclusive"

    reason_codes: list[str] = []
    if plan_task_count == 0:
        reason_codes.append("no_verifiable_tasks")
    if exec_result["status"] != "success":
        reason_codes.append(
            "policy_denied" if policy_status == "denied" else "execution_failed"
        )
    if rerun["exit_code"] not in (None, 0):
        reason_codes.append("rerun_failed")
    if mapping_errors:
        reason_codes.append("mapping_error")
    if monitor_timed_out:
        reason_codes.append("monitor_timed_out")
    if selected and monitor_policy == "on" and monitor is None:
        reason_codes.append("monitor_unavailable")

    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "agent": agent,
        "verification_mode": "smoke_test",
        "source": {
            "fix_plan_path": verification_input["fix_plan_path"],
            "exec_result_path": verification_input["exec_result_path"],
            "exec_result_sha256": json_sha256(exec_result),
            "verification_run_dir": verification_input["verification_run_dir"],
            "analyzer_monitor_path": str(fix_plan["source"].get("monitor_path") or ""),
            "monitor_output_path": monitor_path,
            "smoke_task_source_path": selection["source"]["task_source_path"],
            "smoke_selection_path": selection["source"]["selection_path"],
        },
        "execution": {
            "status": exec_result["status"],
            "policy_status": policy_status,
        },
        "status": status,
        "reason_codes": reason_codes,
        "rerun": {
            **rerun,
            "agent": agent,
            "monitor_policy": monitor_policy,
            "monitor_available": monitor is not None,
            "monitor_timed_out": monitor_timed_out,
        },
        "sampling": {
            "mode": "smoke_test",
            "selection_policy": "stable_hash",
            "limit_per_plan": limit,
            "sampled_task_count": len(selected),
            "plan_task_count": plan_task_count,
            "unsampled_task_count": plan_task_count - len(selected),
            "sampled_task_indexes": [task["original_task_index"] for task in selected],
            "mapping_errors": mapping_errors,
        },
        "new_run_summary": run_summary,
        "plan_results": plan_results,
        "task_results": task_results,
        "unexpected_run_task_results": unexpected,
    }
    validate_verification_result(payload)
    write_json_atomic(output_dir / "verification-result-latest.json", payload)
    return payload


def run_verification_from_paths(
    fix_plan_path: Path,
    exec_result_path: Path,
    verification_run_dir: Path,
    output_dir: Path,
    *,
    agent: str | None = None,
    rerun_command: str | None = None,
    rerun_timeout: int = DEFAULT_RERUN_TIMEOUT_SECONDS,
    monitor_policy: str = "auto",
    monitor_wait_timeout: int = 3600,
    monitor_poll_interval: float = 30.0,
    verification_task_limit_per_plan: int = 2,
    dataset_name: str = "",
    dataset_path: str = "",
    model: str = "",
) -> dict[str, Any]:
    _prepare_verification_output(output_dir)
    payload = build_verification_input(
        fix_plan_path,
        exec_result_path,
        verification_run_dir,
        agent=agent,
        rerun_command=rerun_command,
        rerun_timeout=rerun_timeout,
        monitor_policy=monitor_policy,
        output_dir=output_dir,
        monitor_wait_timeout=monitor_wait_timeout,
        monitor_poll_interval=monitor_poll_interval,
        verification_task_limit_per_plan=verification_task_limit_per_plan,
        dataset_name=dataset_name,
        dataset_path=dataset_path,
        model=model,
    )
    return run_verification(payload, output_dir)
