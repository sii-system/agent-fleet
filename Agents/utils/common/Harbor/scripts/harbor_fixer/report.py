"""Write the deterministic human-readable Harbor Fixer report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_io import write_text_atomic
from .validation import (
    validate_exec_result,
    validate_fix_plan_set,
    validate_verification_result,
)


def _inline(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _action_counts(exec_result: dict[str, Any]) -> dict[str, int]:
    counts = {"success": 0, "failed": 0, "skipped": 0}
    for plan in exec_result["plans"]:
        for action in plan["actions"]:
            counts[str(action["status"])] += 1
    return counts


def _verification_counts(verification_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in verification_result["task_results"]:
        status = str(task["verification_status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def _format_counts(counts: dict[str, int], order: tuple[str, ...]) -> str:
    values = [f"{counts[status]} {status}" for status in order if counts.get(status)]
    return ", ".join(values) if values else "none"


def _plan_actions(fix_plan: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(plan["plan_id"]), str(action["action_id"])): action
        for plan in fix_plan["plans"]
        for action in plan["actions"]
    }


def render_fix_report(
    run_id: str,
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
    verification_result: dict[str, Any],
) -> str:
    """Render validated Fixer artifacts without model-generated content."""

    validate_fix_plan_set(fix_plan)
    validate_exec_result(exec_result)
    validate_verification_result(verification_result)

    action_counts = _action_counts(exec_result)
    verification_counts = _verification_counts(verification_result)
    actions = _plan_actions(fix_plan)
    lines = [
        f"# Harbor Fixer Report: {_inline(run_id)}",
        "",
        "## Summary",
        "",
        (
            f"Fixer execution finished with `{_inline(exec_result['status'])}`; "
            "smoke verification finished with "
            f"`{_inline(verification_result['status'])}`."
        ),
        "",
        "| Item | Result |",
        "| --- | --- |",
        f"| Policy | {_inline(exec_result['policy_status'])} |",
        f"| Execution | {_inline(exec_result['status'])} |",
        f"| Verification | {_inline(verification_result['status'])} |",
        (
            "| Actions | "
            + _format_counts(action_counts, ("success", "failed", "skipped"))
            + " |"
        ),
        (
            "| Reverification | "
            + _format_counts(
                verification_counts,
                (
                    "fixed",
                    "not_fixed",
                    "unknown",
                    "not_complete",
                    "exec_failed",
                    "not_sampled",
                ),
            )
            + " |"
        ),
        "",
        "## Changes Applied",
        "",
    ]

    applied = 0
    remaining: list[str] = []
    for plan in exec_result["plans"]:
        plan_id = str(plan["plan_id"])
        for result in plan["actions"]:
            action_id = str(result["action_id"])
            action = actions[(plan_id, action_id)]
            item = f"`{_inline(plan_id)}/{_inline(action_id)}`"
            purpose = _inline(action["purpose"])
            if result["status"] == "success":
                lines.append(f"- {item}: {purpose}")
                applied += 1
            else:
                remaining.append(
                    f"- Execution `{_inline(result['status'])}` for {item}: {purpose}"
                )
    if not applied:
        lines.append("No Fixer actions completed successfully.")

    for task in verification_result["task_results"]:
        status = str(task["verification_status"])
        if status in {"fixed", "not_sampled"}:
            continue
        identity = task["task"]
        remaining.append(
            f"- Verification `{_inline(status)}` for "
            f"`{_inline(identity['task_name'])}` "
            f"(task `{_inline(identity['task_index'])}`)."
        )
    for error in fix_plan["generation_errors"]:
        remaining.append(f"- Plan generation error: {_inline(error)}")
    for task in fix_plan["unplanned_tasks"]:
        remaining.append(
            f"- No Fix Plan was generated for `{_inline(task['task_name'])}` "
            f"(task `{_inline(task['task_index'])}`)."
        )

    lines.extend(["", "## Remaining Issues", ""])
    lines.extend(remaining or ["No remaining issues were reported."])
    return "\n".join(lines) + "\n"


def write_fix_report(
    run_id: str,
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
    verification_result: dict[str, Any],
    output_path: Path,
) -> None:
    write_text_atomic(
        output_path,
        render_fix_report(run_id, fix_plan, exec_result, verification_result),
    )
