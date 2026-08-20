"""Derive verification outcomes from execution and Harbor task state."""

from __future__ import annotations

from typing import Any


def verification_status(exec_status: str, run_record: dict[str, Any] | None) -> str:
    if exec_status != "success":
        return "exec_failed"
    if run_record is None:
        return "not_sampled"
    return {
        "complete_success": "fixed",
        "complete_failed": "not_fixed",
        "complete_unknown": "unknown",
        "not_complete": "not_complete",
    }[str(run_record["task_complete_status"])]


def exec_failure_reason(exec_status: str, policy_status: str) -> str | None:
    if exec_status == "success":
        return None
    return "policy_denied" if policy_status == "denied" else "execution_failed"


def aggregate_status(statuses: list[str], rerun_exit_code: int | None) -> str:
    statuses = [status for status in statuses if status != "not_sampled"]
    if rerun_exit_code not in (None, 0) or not statuses:
        return "inconclusive"
    if "exec_failed" in statuses:
        return "exec_failed"
    if all(status == "fixed" for status in statuses):
        return "fixed"
    if "fixed" in statuses:
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in statuses):
        return "inconclusive"
    return "not_fixed"


def plan_status(statuses: list[str]) -> str:
    statuses = [status for status in statuses if status != "not_sampled"]
    if "exec_failed" in statuses:
        return "exec_failed"
    if statuses and all(status == "fixed" for status in statuses):
        return "fixed"
    if "fixed" in statuses:
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in statuses):
        return "inconclusive"
    return "not_fixed" if statuses else "inconclusive"
