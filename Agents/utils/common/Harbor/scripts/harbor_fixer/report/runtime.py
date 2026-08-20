"""Summary-agent runtime for Harbor Fixer reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor_pi_runtime import sleep_before_retry

from ..agent_invocation import AgentInvoker
from ..artifact_io import write_text
from ..prompts import build_validation_retry_prompt
from ..validation import (
    ValidationError,
    parse_strict_json_object,
    validate_report_summary,
)
from .prompt import REPORT_MAIN_AGENT_PROMPT


def _fallback_summary(
    summary_input: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    new_run = (
        summary_input.get("new_run")
        if isinstance(summary_input.get("new_run"), dict)
        else {}
    )
    sampling = (
        new_run.get("sampling") if isinstance(new_run.get("sampling"), dict) else {}
    )
    task_results = [
        item for item in summary_input.get("task_results", []) if isinstance(item, dict)
    ]
    sampled = [item for item in task_results if item.get("sampled")]
    plan_task_count = int(sampling.get("plan_task_count") or len(task_results))
    sampled_task_count = int(sampling.get("sampled_task_count") or len(sampled))
    unsampled_task_count = int(
        sampling.get("unsampled_task_count")
        or max(0, plan_task_count - sampled_task_count)
    )
    fixed_count = sum(item.get("verification_status") == "fixed" for item in sampled)
    not_fixed_count = sum(
        item.get("verification_status") == "not_fixed" for item in sampled
    )
    other_count = len(sampled) - fixed_count - not_fixed_count
    unsampled_exec_failed_count = sum(
        not item.get("sampled") and item.get("verification_status") == "exec_failed"
        for item in task_results
    )
    mode = str(new_run.get("verification_mode") or "verification run")
    text = (
        "Deterministic fallback summary: report-main-agent was unavailable. "
        f"Fixer recorded {mode} results for {sampled_task_count} of {plan_task_count} planned task(s). "
        f"Among sampled tasks, verifier labels were: {fixed_count} fixed, "
        f"{not_fixed_count} not_fixed, and {other_count} other or inconclusive. "
        f"{unsampled_task_count} task(s) were not sampled and have no rerun result."
    )
    if unsampled_exec_failed_count:
        text += (
            f" This includes {unsampled_exec_failed_count} unsampled task(s) labeled "
            "exec_failed from Fixer execution; they have no rerun result."
        )
    if not summary_input.get("old_run", {}).get("monitor_available"):
        text += " Baseline monitor data was unavailable, so no before/after comparison is claimed."
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary",
        "status": "failed",
        "text": text,
        "highlights": [
            f"sampled task labels: {fixed_count} fixed, {not_fixed_count} not_fixed, {other_count} other or inconclusive",
            f"unsampled tasks: {unsampled_task_count}",
            f"unsampled exec_failed tasks: {unsampled_exec_failed_count}",
        ],
        "caveats": [
            "summary generated without report-main-agent due to summary generation failure",
            *summary_input.get("caveats", []),
        ],
        "generation_errors": errors,
    }


def generate_report_summary(
    invoker: AgentInvoker,
    summary_input: dict[str, Any],
    output_dir: Path,
    *,
    max_attempts: int = 2,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[dict[str, Any]] = []
    raw_paths: list[str] = []
    prompt = REPORT_MAIN_AGENT_PROMPT
    for attempt in range(1, max_attempts + 1):
        raw = ""
        try:
            raw = invoker.invoke(
                prompt, summary_input, attempt=attempt, label="report-main-agent"
            )
        except Exception as exc:  # noqa: BLE001 - provider retry boundary
            error = f"{type(exc).__name__}: report summary invocation failed"
        else:
            raw_path = (
                output_dir / "raw-report-main-agent-output" / f"attempt-{attempt}.txt"
            )
            write_text(raw_path, raw)
            raw_paths.append(str(raw_path))
            try:
                payload = parse_strict_json_object(raw)
                validate_report_summary(payload)
                if payload["status"] != "success":
                    raise ValidationError("report summary status must be success")
                if payload["generation_errors"]:
                    raise ValidationError(
                        "report summary generation_errors must be empty"
                    )
                payload["generation_errors"] = list(errors)
            except ValidationError as exc:
                error = str(exc)
            else:
                return payload, raw_paths
            if raw and attempt < max_attempts:
                prompt = build_validation_retry_prompt(
                    base_prompt=REPORT_MAIN_AGENT_PROMPT,
                    previous_output=raw,
                    validation_error=error,
                )
        errors.append(
            {"stage": "report_main_agent", "attempt": attempt, "error": error}
        )
        if attempt < max_attempts:
            sleep_before_retry(attempt)
    return _fallback_summary(summary_input, errors), raw_paths
