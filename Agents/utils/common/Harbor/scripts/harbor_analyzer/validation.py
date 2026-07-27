"""Validate Analyzer final JSON without doing semantic root-cause analysis."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .identity import task_identity, task_key
from .taxonomy import (
    ANALYSIS_STATUSES,
    ENV_INFRA_CLASSES,
    FAILURE_STAGES,
    FINAL_CLASSES,
    RECOMMENDED_EVENTS,
    SCOPES,
    UNKNOWN_ROOT_CAUSE,
    allowed_failure_stages_for_root_cause,
    allowed_scopes_for_root_cause,
    expected_final_class_for_root_cause,
)

ROOT_CAUSE_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,11}$")

REQUIRED_TASK_REPORT_KEYS = {
    "task",
    "analysis_status",
    "final_class",
    "failure_stage",
    "root_cause_code",
    "root_cause_summary",
    "scope",
    "confidence",
    "observations",
    "reasoning_summary",
    "alternatives_considered",
    "recommended_events",
    "fix_references",
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _item_task(item: dict[str, Any]) -> dict[str, Any] | None:
    task = item.get("task")
    return task if isinstance(task, dict) else None


def _validate_line_reference(item: dict[str, Any], prefix: str, errors: list[str]) -> None:
    path = item.get("path")
    if not _nonempty_string(path):
        errors.append(f"{prefix}_path_required")
    else:
        if "..." in path:
            errors.append(f"{prefix}_path_must_not_contain_ellipsis")
        if not path.startswith("/"):
            errors.append(f"{prefix}_path_must_be_absolute")
    line_start = item.get("line_start")
    line_end = item.get("line_end")
    if (
        isinstance(line_start, bool)
        or isinstance(line_end, bool)
        or not isinstance(line_start, int)
        or not isinstance(line_end, int)
        or line_start <= 0
        or line_end < line_start
    ):
        errors.append(f"{prefix}_invalid_line_range")
    if not _nonempty_string(item.get("reason")):
        errors.append(f"{prefix}_reason_required")
    if not _nonempty_string(item.get("fact")):
        errors.append(f"{prefix}_fact_required")


def _normalize_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(Path(value).expanduser())


def _range_fully_covered(
    line_start: int,
    line_end: int,
    ranges: list[tuple[int, int]],
) -> bool:
    next_uncovered = line_start
    for accessed_start, accessed_end in sorted(ranges):
        if accessed_end < next_uncovered:
            continue
        if accessed_start > next_uncovered:
            return False
        next_uncovered = accessed_end + 1
        if next_uncovered > line_end:
            return True
    return False


def _load_tool_access_ranges(tool_access_audit_path: str | Path | None) -> list[tuple[str, int, int]]:
    if tool_access_audit_path is None:
        return []
    path = Path(tool_access_audit_path)
    if not path.is_file():
        return []
    ranges: list[tuple[str, int, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("allowed") is not True:
            continue
        if record.get("tool") == "read":
            line_start = record.get("line_start")
            line_end = record.get("line_end")
            path_value = record.get("resolved_path") or record.get("absolute_path")
            if isinstance(path_value, str) and isinstance(line_start, int) and isinstance(line_end, int):
                ranges.append((_normalize_path(path_value), line_start, line_end))
        elif record.get("tool") == "grep":
            matches = record.get("matches")
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                path_value = match.get("resolved_path") or match.get("path")
                line_start = match.get("line_start")
                line_end = match.get("line_end")
                if isinstance(path_value, str) and isinstance(line_start, int) and isinstance(line_end, int):
                    ranges.append((_normalize_path(path_value), line_start, line_end))
    return ranges


def _validate_fix_references_are_grounded(
    report: dict[str, Any],
    *,
    index: int,
    tool_access_audit_path: str | Path | None,
    errors: list[str],
) -> None:
    if tool_access_audit_path is None:
        return
    if report.get("analysis_status") != "analysis_complete" or report.get("final_class") not in ENV_INFRA_CLASSES:
        return
    fix_references = report.get("fix_references")
    if not isinstance(fix_references, list) or not fix_references:
        return
    accessed_ranges = _load_tool_access_ranges(tool_access_audit_path)
    for reference_index, reference in enumerate(fix_references):
        if not isinstance(reference, dict):
            continue
        path = reference.get("path")
        line_start = reference.get("line_start")
        line_end = reference.get("line_end")
        if not isinstance(path, str) or not isinstance(line_start, int) or not isinstance(line_end, int):
            continue
        normalized_path = _normalize_path(path)
        grounded = _range_fully_covered(
            line_start,
            line_end,
            [
                (accessed_start, accessed_end)
                for accessed_path, accessed_start, accessed_end in accessed_ranges
                if accessed_path == normalized_path
            ],
        )
        if not grounded:
            errors.append(f"task_{index}_fix_reference_{reference_index}_not_grounded_in_tool_audit")


def _validate_task_report(
    report: dict[str, Any],
    index: int,
    errors: list[str],
    *,
    tool_access_audit_path: str | Path | None = None,
) -> tuple[str, str, str] | None:
    missing = sorted(REQUIRED_TASK_REPORT_KEYS - set(report))
    if missing:
        errors.append(f"task_{index}_missing_keys={','.join(missing)}")

    task = _item_task(report)
    if task is None:
        errors.append(f"task_{index}_task_required")
        key = None
    else:
        identity = task_identity(task)
        if not identity["task_index"] or not identity["task_name"]:
            errors.append(f"task_{index}_identity_incomplete")
        key = task_key(identity)

    analysis_status = report.get("analysis_status")
    if analysis_status not in ANALYSIS_STATUSES:
        errors.append(f"task_{index}_invalid_analysis_status")

    final_class = report.get("final_class")
    if final_class not in FINAL_CLASSES:
        errors.append(f"task_{index}_invalid_final_class")

    failure_stage = report.get("failure_stage")
    if failure_stage not in FAILURE_STAGES:
        errors.append(f"task_{index}_invalid_failure_stage")
    scope = report.get("scope")
    if scope not in SCOPES:
        errors.append(f"task_{index}_invalid_scope")

    root_cause = report.get("root_cause_code")
    if not isinstance(root_cause, str) or not ROOT_CAUSE_CODE_RE.fullmatch(root_cause):
        errors.append(f"task_{index}_invalid_root_cause_code")
    else:
        expected_class = expected_final_class_for_root_cause(root_cause)
        if expected_class is None:
            errors.append(f"task_{index}_root_cause_code_not_in_taxonomy")
        elif final_class in FINAL_CLASSES and final_class != expected_class:
            errors.append(
                f"task_{index}_root_cause_class_mismatch expected={expected_class} observed={final_class}"
            )
        elif expected_class is not None:
            allowed_stages = allowed_failure_stages_for_root_cause(root_cause)
            if failure_stage in FAILURE_STAGES and failure_stage not in allowed_stages:
                errors.append(
                    f"task_{index}_root_cause_failure_stage_mismatch allowed={','.join(allowed_stages)} observed={failure_stage}"
                )
            allowed_scopes = allowed_scopes_for_root_cause(root_cause)
            if scope in SCOPES and scope not in allowed_scopes:
                errors.append(
                    f"task_{index}_root_cause_scope_mismatch allowed={','.join(allowed_scopes)} observed={scope}"
                )
    if final_class == "unknown":
        if root_cause != UNKNOWN_ROOT_CAUSE:
            errors.append(f"task_{index}_unknown_root_cause_mismatch")
        if failure_stage != "unknown":
            errors.append(f"task_{index}_unknown_failure_stage_mismatch")
    elif final_class == "success":
        if failure_stage != "none":
            errors.append(f"task_{index}_success_failure_stage_must_be_none")
    elif root_cause == UNKNOWN_ROOT_CAUSE:
        errors.append(f"task_{index}_known_class_cannot_use_unknown_root_cause")
    if analysis_status == "analysis_failed" and final_class != "unknown":
        errors.append(f"task_{index}_failed_analysis_must_be_unknown")

    if not _nonempty_string(report.get("root_cause_summary")):
        errors.append(f"task_{index}_root_cause_summary_required")
    confidence = report.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        errors.append(f"task_{index}_confidence_must_be_number")
    elif not 0 <= float(confidence) <= 1:
        errors.append(f"task_{index}_confidence_out_of_range")

    observations = report.get("observations")
    if not isinstance(observations, list):
        errors.append(f"task_{index}_observations_must_be_array")
    elif analysis_status == "analysis_complete" and final_class in {"success", "env_fail", "infra_fail", "model_fail"} and not observations:
        errors.append(f"task_{index}_known_class_requires_observation")

    if not _nonempty_string(report.get("reasoning_summary")):
        errors.append(f"task_{index}_reasoning_summary_required")

    alternatives = report.get("alternatives_considered")
    if not isinstance(alternatives, list):
        errors.append(f"task_{index}_alternatives_must_be_array")

    events = report.get("recommended_events")
    if not isinstance(events, list):
        errors.append(f"task_{index}_recommended_events_must_be_array")
    elif any(event not in RECOMMENDED_EVENTS for event in events):
        errors.append(f"task_{index}_invalid_recommended_event")
    elif len(events) != len(set(events)):
        errors.append(f"task_{index}_duplicate_recommended_event")
    # Analyzer reports are always user-visible; no task silently suppresses notification.
    elif events != ["notify_user"]:
        errors.append(f"task_{index}_recommended_events_must_notify_user_only")

    fix_references = report.get("fix_references")
    if not isinstance(fix_references, list):
        errors.append(f"task_{index}_fix_references_must_be_array")
    else:
        if analysis_status == "analysis_complete" and final_class in ENV_INFRA_CLASSES and not fix_references:
            errors.append(f"task_{index}_env_infra_requires_fix_reference")
        for reference_index, reference in enumerate(fix_references):
            if not isinstance(reference, dict):
                errors.append(f"task_{index}_fix_reference_{reference_index}_must_be_object")
                continue
            _validate_line_reference(
                reference,
                f"task_{index}_fix_reference_{reference_index}",
                errors,
            )
    _validate_fix_references_are_grounded(
        report,
        index=index,
        tool_access_audit_path=tool_access_audit_path,
        errors=errors,
    )
    return key


def validate_task_analysis(
    task_json: dict[str, Any],
    *,
    handover_id: str,
    expected_task: dict[str, Any],
    tool_access_audit_path: str | Path | None = None,
) -> list[str]:
    """Validate one Pi-produced task analysis."""

    errors: list[str] = []
    if task_json.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_must_be_2")
    if task_json.get("kind") != "harbor_task_root_cause_analysis":
        errors.append("kind_must_be_harbor_task_root_cause_analysis")
    if task_json.get("handover_id") != handover_id:
        errors.append("handover_id_mismatch")
    if task_json.get("analysis_status") != "analysis_complete":
        errors.append("analysis_status_must_be_analysis_complete")

    key = _validate_task_report(
        task_json,
        0,
        errors,
        tool_access_audit_path=tool_access_audit_path,
    )
    expected_key = task_key(expected_task)
    if key is not None and key != expected_key:
        errors.append(f"task_identity_mismatch expected={expected_key} observed={key}")
    return errors


def validate_final_json(
    final_json: dict[str, Any],
    *,
    handover_id: str,
    handover_tasks: list[dict[str, Any]],
) -> list[str]:
    """Validate structure and cross-references only.

    This intentionally does not classify root causes or reinterpret LLM output.
    """

    errors: list[str] = []
    if final_json.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_must_be_2")
    if final_json.get("kind") != "harbor_analyzer_final":
        errors.append("kind_must_be_harbor_analyzer_final")
    if final_json.get("handover_id") != handover_id:
        errors.append("handover_id_mismatch")

    benchmark_report = final_json.get("benchmark_report")
    if not isinstance(benchmark_report, dict):
        errors.append("benchmark_report_must_be_object")
        benchmark_report = {}
    if benchmark_report.get("schema_version") != SCHEMA_VERSION:
        errors.append("benchmark_report_schema_version_must_be_2")
    if benchmark_report.get("kind") != "harbor_benchmark_root_cause_report":
        errors.append("benchmark_report_kind_invalid")
    if benchmark_report.get("handover_id") != handover_id:
        errors.append("benchmark_report_handover_id_mismatch")

    report_tasks = benchmark_report.get("tasks")
    if not isinstance(report_tasks, list):
        errors.append("benchmark_report_tasks_must_be_array")
        report_tasks = []

    expected_keys = {task_key(task) for task in handover_tasks}
    observed_keys: set[tuple[str, str, str]] = set()
    report_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, item in enumerate(report_tasks):
        if not isinstance(item, dict):
            errors.append(f"benchmark_report_task_{index}_must_be_object")
            continue
        key = _validate_task_report(item, index, errors)
        if key is None:
            continue
        if key in observed_keys:
            errors.append(f"benchmark_report_task_{index}_duplicate_identity")
        observed_keys.add(key)
        report_by_key[key] = item

    missing = sorted(expected_keys - observed_keys)
    extra = sorted(observed_keys - expected_keys)
    if missing:
        errors.append(f"benchmark_report_missing_tasks={missing}")
    if extra:
        errors.append(f"benchmark_report_extra_tasks={extra}")

    env_infra = final_json.get("env_infra_tasks")
    if not isinstance(env_infra, dict):
        errors.append("env_infra_tasks_must_be_object")
        env_infra = {}
    if env_infra.get("schema_version") != SCHEMA_VERSION:
        errors.append("env_infra_tasks_schema_version_must_be_2")
    if env_infra.get("kind") != "harbor_env_infra_task_list":
        errors.append("env_infra_tasks_kind_invalid")
    if env_infra.get("handover_id") != handover_id:
        errors.append("env_infra_tasks_handover_id_mismatch")

    env_tasks = env_infra.get("tasks")
    if not isinstance(env_tasks, list):
        errors.append("env_infra_tasks_tasks_must_be_array")
        env_tasks = []
    env_keys: set[tuple[str, str, str]] = set()
    for index, item in enumerate(env_tasks):
        if not isinstance(item, dict):
            errors.append(f"env_infra_task_{index}_must_be_object")
            continue
        task = _item_task(item)
        if task is None:
            errors.append(f"env_infra_task_{index}_task_required")
            continue
        key = task_key(task)
        if key not in report_by_key:
            errors.append(f"env_infra_task_{index}_not_in_benchmark_report")
            continue
        final_class = item.get("final_class")
        report_class = report_by_key[key].get("final_class")
        if final_class not in ENV_INFRA_CLASSES:
            errors.append(f"env_infra_task_{index}_final_class_not_env_infra")
        if report_class != final_class:
            errors.append(f"env_infra_task_{index}_final_class_mismatch")
        env_keys.add(key)

    fix_index = final_json.get("fix_line_index")
    if not isinstance(fix_index, list):
        errors.append("fix_line_index_must_be_array")
        fix_index = []
    fix_keys: set[tuple[str, str, str]] = set()
    for index, item in enumerate(fix_index):
        if not isinstance(item, dict):
            errors.append(f"fix_line_index_{index}_must_be_object")
            continue
        task = _item_task(item)
        if task is None:
            errors.append(f"fix_line_index_{index}_task_required")
            continue
        key = task_key(task)
        if key not in env_keys:
            errors.append(f"fix_line_index_{index}_task_not_env_infra")
        _validate_line_reference(item, f"fix_line_index_{index}", errors)
        fix_keys.add(key)

    missing_fix = sorted(env_keys - fix_keys)
    if missing_fix:
        errors.append(f"env_infra_tasks_missing_fix_line_index={missing_fix}")
    report_env_infra = sorted(
        {
            key
            for key, item in report_by_key.items()
            if item.get("final_class") in ENV_INFRA_CLASSES
        }
        - env_keys
    )
    if report_env_infra:
        errors.append(f"benchmark_report_env_infra_not_in_env_infra_tasks={report_env_infra}")
    return errors
