"""Lightweight validation helpers for Harbor Fixer MVP artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import shlex
from typing import Any


class ValidationError(ValueError):
    """Raised when an external artifact or agent output is unusable."""


ENV_INFRA_CLASSES = {"env_fail", "infra_fail"}
ANALYZER_SCOPES = {"task", "benchmark", "host"}
FIX_SCOPES = {"task", "benchmark", "host"}
SUMMARY_SCOPES = {"task", "benchmark", "host", "unknown"}
SCOPE_AGREEMENTS = {"agree", "unclear", "disagree"}
CONFIDENCE_LABELS = {"high", "medium", "low"}
PLAN_SCOPE_RELATIONS = {"same", "narrower", "broader", "mixed"}
SCOPE_RANK = {"task": 0, "benchmark": 1, "host": 2}
FIX_PLAN_SCHEMA_VERSION = 2
ACTION_TYPES = {"command", "file_edit"}

EXEC_STATUSES = {"success", "partial_failed", "failed"}
EXEC_COMMAND_STATUSES = {"success", "failed", "skipped"}
EXEC_POLICY_STATUSES = {"allowed", "denied"}
HARBOR_AGENTS = {"claude-code", "opencode", "oracle"}
MONITOR_POLICIES = {"auto", "on", "off"}
VERIFICATION_STATUSES = {
    "fixed",
    "partially_fixed",
    "not_fixed",
    "inconclusive",
    "exec_failed",
}
TASK_VERIFICATION_STATUSES = {
    "fixed",
    "not_fixed",
    "unknown",
    "not_complete",
    "not_sampled",
    "exec_failed",
}
TASK_COMPLETE_STATUSES = {
    "complete_success",
    "complete_failed",
    "complete_unknown",
    "not_complete",
}
EXEC_FAILURE_REASONS = {"execution_failed", "policy_denied"}
VERIFICATION_REASON_CODES = EXEC_FAILURE_REASONS | {
    "mapping_error",
    "monitor_timed_out",
    "monitor_unavailable",
    "no_verifiable_tasks",
    "rerun_failed",
}
INCONCLUSIVE_VERIFICATION_REASONS = {
    "mapping_error",
    "monitor_timed_out",
    "monitor_unavailable",
    "rerun_failed",
}


def json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    return value


def require_string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{name} must be non-empty")
    return value


def require_enum(value: Any, name: str, allowed: set[str]) -> str:
    text = require_string(value, name)
    if text not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return text


def _require_exact_fields(value: dict[str, Any], name: str, fields: set[str]) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise ValidationError(f"{name} fields are invalid: {'; '.join(details)}")


def _require_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    text = require_string(value, name, allow_empty=allow_empty)
    if "\x00" in text:
        raise ValidationError(f"{name} must not contain NUL")
    return text


def _validate_fix_action(value: Any, name: str) -> str:
    action = require_dict(value, name)
    action_type = require_enum(
        action.get("action_type"), f"{name}.action_type", ACTION_TYPES
    )
    common_fields = {
        "action_id",
        "action_type",
        "cwd",
        "purpose",
        "expected_effect",
    }
    action_fields = (
        common_fields | {"executable", "arguments"}
        if action_type == "command"
        else common_fields | {"path", "edit"}
    )
    _require_exact_fields(action, name, action_fields)
    action_id = _require_text(action.get("action_id"), f"{name}.action_id")
    _require_text(action.get("cwd"), f"{name}.cwd")
    _require_text(action.get("purpose"), f"{name}.purpose")
    _require_text(action.get("expected_effect"), f"{name}.expected_effect")
    if action_type == "command":
        executable = _require_text(action.get("executable"), f"{name}.executable")
        if any(character.isspace() for character in executable):
            raise ValidationError(f"{name}.executable must be one token")
        for index, argument in enumerate(
            require_list(action.get("arguments"), f"{name}.arguments")
        ):
            _require_text(argument, f"{name}.arguments[{index}]", allow_empty=True)
        return action_id

    _require_text(action.get("path"), f"{name}.path")
    edit = require_dict(action.get("edit"), f"{name}.edit")
    _require_exact_fields(
        edit,
        f"{name}.edit",
        {"kind", "old_text", "new_text", "expected_replacements"},
    )
    if edit.get("kind") != "replace_text":
        raise ValidationError(f"{name}.edit.kind must be replace_text")
    old_text = _require_text(edit.get("old_text"), f"{name}.edit.old_text")
    new_text = _require_text(
        edit.get("new_text"), f"{name}.edit.new_text", allow_empty=True
    )
    if old_text == new_text:
        raise ValidationError(f"{name}.edit must change the matched text")
    replacements = edit.get("expected_replacements")
    if isinstance(replacements, bool) or not isinstance(replacements, int):
        raise ValidationError(f"{name}.edit.expected_replacements must be an integer")
    if replacements < 1:
        raise ValidationError(f"{name}.edit.expected_replacements must be positive")
    return action_id


def task_key(task: dict[str, Any]) -> tuple[str, str, str]:
    attempt_id = task.get("attempt_id")
    return (
        str(task.get("task_index") or ""),
        str(task.get("task_name") or ""),
        "" if attempt_id is None else str(attempt_id),
    )


def _require_exact_task_identity(
    task: dict[str, Any],
    expected: dict[str, Any],
    name: str,
) -> None:
    if any(
        task[field] != expected[field]
        for field in ("task_index", "task_name", "attempt_id")
    ):
        raise ValidationError(f"{name} identity does not match task summary")


def _scope_relation(fix_scope: str, analyzer_scopes: set[str]) -> str:
    differences = {SCOPE_RANK[fix_scope] - SCOPE_RANK[scope] for scope in analyzer_scopes}
    if differences == {0}:
        return "same"
    if min(differences) >= 0:
        return "broader"
    if max(differences) <= 0:
        return "narrower"
    return "mixed"


def _validate_task_identity(task: dict[str, Any], name: str) -> tuple[str, str, str]:
    require_string(task.get("task_index"), f"{name}.task_index")
    require_string(task.get("task_name"), f"{name}.task_name")
    if "attempt_id" not in task:
        raise ValidationError(f"{name}.attempt_id is required")
    return task_key(task)


def _reject_json_constant(value: str) -> Any:
    raise ValidationError(f"invalid JSON constant: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValidationError(f"invalid JSON number: {value}")
    return parsed


def parse_strict_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc
    return require_dict(payload, "model output")


def _check_kind(payload: dict[str, Any], *, version: int, kind: str, name: str) -> None:
    require_dict(payload, name)
    if payload.get("schema_version") != version:
        raise ValidationError(f"{name} schema_version must be {version}")
    if payload.get("kind") != kind:
        raise ValidationError(f"{name} kind must be {kind}")


def validate_analyzer_manifest(payload: dict[str, Any]) -> None:
    _check_kind(
        payload,
        version=2,
        kind="harbor_analyzer_latest_artifacts",
        name="analyzer artifact manifest",
    )
    require_string(payload.get("run_id"), "analyzer artifact manifest run_id")
    if "monitor_path" in payload:
        require_string(
            payload["monitor_path"],
            "analyzer artifact manifest monitor_path",
            allow_empty=True,
        )
    seen_handovers: set[str] = set()
    for index, item in enumerate(
        require_list(payload.get("publications"), "analyzer artifact manifest publications")
    ):
        publication = require_dict(item, f"publications[{index}]")
        handover_id = require_string(
            publication.get("handover_id"),
            f"publications[{index}].handover_id",
        )
        require_string(
            publication.get("publication_id"),
            f"publications[{index}].publication_id",
        )
        if handover_id in seen_handovers:
            raise ValidationError(f"duplicate analyzer publication for handover_id={handover_id}")
        seen_handovers.add(handover_id)


def validate_env_infra_tasks(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=2, kind="harbor_env_infra_task_list", name="env/infra task list")
    for index, item in enumerate(require_list(payload.get("tasks"), "env/infra tasks")):
        item_obj = require_dict(item, f"env/infra tasks[{index}]")
        task = require_dict(item_obj.get("task"), f"env/infra tasks[{index}].task")
        require_string(task.get("task_index"), f"env/infra tasks[{index}].task.task_index")
        require_string(task.get("task_name"), f"env/infra tasks[{index}].task.task_name")
        if "attempt_id" not in task:
            raise ValidationError(f"env/infra tasks[{index}].task.attempt_id is required")
        require_enum(item_obj.get("final_class"), f"env/infra tasks[{index}].final_class", ENV_INFRA_CLASSES)
        require_string(item_obj.get("failure_stage"), f"env/infra tasks[{index}].failure_stage")
        require_enum(item_obj.get("scope"), f"env/infra tasks[{index}].scope", ANALYZER_SCOPES)
        confidence = item_obj.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValidationError(f"env/infra tasks[{index}].confidence must be a number")
        if not 0 <= confidence <= 1:
            raise ValidationError(f"env/infra tasks[{index}].confidence must be between 0 and 1")
        require_string(item_obj.get("root_cause_code"), f"env/infra tasks[{index}].root_cause_code")
        require_string(
            item_obj.get("root_cause_summary"),
            f"env/infra tasks[{index}].root_cause_summary",
        )


def validate_fix_line_index(records: list[dict[str, Any]]) -> None:
    for index, record in enumerate(records):
        _check_kind(
            record,
            version=2,
            kind="harbor_fix_line_reference",
            name=f"fix-line-index[{index}]",
        )
        task = require_dict(record.get("task"), f"fix-line-index[{index}].task")
        require_string(task.get("task_index"), f"fix-line-index[{index}].task.task_index")
        require_string(task.get("task_name"), f"fix-line-index[{index}].task.task_name")
        if "attempt_id" not in task:
            raise ValidationError(f"fix-line-index[{index}].task.attempt_id is required")
        require_string(record.get("root_cause_code"), f"fix-line-index[{index}].root_cause_code")
        require_string(record.get("path"), f"fix-line-index[{index}].path")
        require_string(record.get("fact"), f"fix-line-index[{index}].fact")
        require_string(record.get("reason"), f"fix-line-index[{index}].reason")
        line_start = record.get("line_start")
        line_end = record.get("line_end")
        if (
            isinstance(line_start, bool)
            or isinstance(line_end, bool)
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or line_start <= 0
            or line_end < line_start
        ):
            raise ValidationError(f"fix-line-index[{index}] has an invalid line range")


def validate_task_input(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_task_input", name="task input")
    require_string(require_dict(payload.get("task"), "task input task").get("task_index"), "task.task_index")
    analyzer = require_dict(payload.get("analyzer_result"), "analyzer_result")
    require_enum(analyzer.get("final_class"), "analyzer_result.final_class", ENV_INFRA_CLASSES)
    require_enum(analyzer.get("scope"), "analyzer_result.scope", ANALYZER_SCOPES)
    require_string(analyzer.get("root_cause_code"), "analyzer_result.root_cause_code")
    require_string(analyzer.get("root_cause_summary"), "analyzer_result.root_cause_summary")
    if not require_list(payload.get("evidence"), "evidence"):
        raise ValidationError("evidence must be non-empty")


def validate_task_summary(
    payload: dict[str, Any],
    expected_input: dict[str, Any] | None = None,
) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_task_summary", name="task summary")
    task = require_dict(payload.get("task"), "task summary task")
    _validate_task_identity(task, "task")
    if expected_input is not None and task != expected_input["task"]:
        raise ValidationError("task summary identity does not match task input")
    alignment = require_dict(payload.get("analyzer_alignment"), "analyzer_alignment")
    require_enum(alignment.get("final_class"), "analyzer_alignment.final_class", ENV_INFRA_CLASSES)
    require_enum(alignment.get("analyzer_scope"), "analyzer_alignment.analyzer_scope", ANALYZER_SCOPES)
    require_string(alignment.get("root_cause_code"), "analyzer_alignment.root_cause_code")
    require_enum(alignment.get("scope_agreement"), "analyzer_alignment.scope_agreement", SCOPE_AGREEMENTS)
    if expected_input is not None:
        analyzer = expected_input["analyzer_result"]
        copied_fields = {
            "final_class": analyzer["final_class"],
            "analyzer_scope": analyzer["scope"],
            "root_cause_code": analyzer["root_cause_code"],
        }
        if any(alignment[name] != value for name, value in copied_fields.items()):
            raise ValidationError("task summary analyzer alignment does not match task input")
    require_string(payload.get("root_cause_summary"), "root_cause_summary")
    require_string(payload.get("reasoning_summary"), "reasoning_summary")
    expected_evidence = expected_input["evidence"] if expected_input is not None else None
    for index, item in enumerate(
        require_list(payload.get("strongest_evidence"), "strongest_evidence")
    ):
        evidence = require_dict(item, f"strongest_evidence[{index}]")
        path = require_string(evidence.get("path"), f"strongest_evidence[{index}].path")
        line_start = evidence.get("line_start")
        line_end = evidence.get("line_end")
        if (
            isinstance(line_start, bool)
            or isinstance(line_end, bool)
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or line_start <= 0
            or line_end < line_start
        ):
            raise ValidationError(f"strongest_evidence[{index}] has an invalid line range")
        require_string(evidence.get("summary"), f"strongest_evidence[{index}].summary")
        if expected_evidence is not None and not any(
            path == source["path"]
            and line_start >= source["line_start"]
            and line_end <= source["line_end"]
            for source in expected_evidence
        ):
            raise ValidationError(f"strongest_evidence[{index}] is absent from task input")
    fix_direction = require_dict(payload.get("fix_direction"), "fix_direction")
    require_enum(fix_direction.get("suggested_scope"), "fix_direction.suggested_scope", SUMMARY_SCOPES)
    require_string(fix_direction.get("summary"), "fix_direction.summary")
    require_string(
        fix_direction.get("why_this_should_fix_it"),
        "fix_direction.why_this_should_fix_it",
    )
    require_string(payload.get("grouping_key_hint"), "grouping_key_hint")
    require_enum(payload.get("confidence"), "confidence", CONFIDENCE_LABELS)
    require_list(payload.get("unknowns"), "unknowns")


def validate_fix_plan_set(
    payload: dict[str, Any],
    *,
    expected_source: dict[str, Any] | None = None,
    expected_task_summaries: list[dict[str, Any]] | None = None,
) -> None:
    _check_kind(
        payload,
        version=FIX_PLAN_SCHEMA_VERSION,
        kind="harbor_fixer_fix_plan_set",
        name="fix plan set",
    )
    source = require_dict(payload.get("source"), "source")
    if expected_source is not None and source != expected_source:
        raise ValidationError("fix plan source does not match main agent input")
    expected_tasks = (
        {task_key(summary["task"]): summary for summary in expected_task_summaries}
        if expected_task_summaries is not None
        else None
    )
    covered_tasks: set[tuple[str, str, str]] = set()
    seen_plan_ids: set[str] = set()
    for index, plan in enumerate(require_list(payload.get("plans"), "plans")):
        plan_obj = require_dict(plan, f"plans[{index}]")
        _require_exact_fields(
            plan_obj,
            f"plans[{index}]",
            {
                "plan_id",
                "fix_scope",
                "analyzer_scope_comparison",
                "task_list",
                "actions",
                "fix_reason",
                "verification_hint",
            },
        )
        plan_id = require_string(plan_obj.get("plan_id"), f"plans[{index}].plan_id")
        if plan_id in seen_plan_ids:
            raise ValidationError(f"duplicate plan_id: {plan_id}")
        seen_plan_ids.add(plan_id)
        fix_scope = require_enum(
            plan_obj.get("fix_scope"),
            f"plans[{index}].fix_scope",
            FIX_SCOPES,
        )
        comparison = require_dict(
            plan_obj.get("analyzer_scope_comparison"),
            f"plans[{index}].analyzer_scope_comparison",
        )
        analyzer_scopes = require_list(
            comparison.get("analyzer_scopes"),
            f"plans[{index}].analyzer_scope_comparison.analyzer_scopes",
        )
        if not analyzer_scopes:
            raise ValidationError(
                f"plans[{index}].analyzer_scope_comparison.analyzer_scopes must be non-empty"
            )
        for scope_index, scope in enumerate(analyzer_scopes):
            require_enum(
                scope,
                f"plans[{index}].analyzer_scope_comparison.analyzer_scopes[{scope_index}]",
                ANALYZER_SCOPES,
            )
        relation = require_enum(
            comparison.get("relation"),
            f"plans[{index}].analyzer_scope_comparison.relation",
            PLAN_SCOPE_RELATIONS,
        )
        require_string(
            comparison.get("reason"),
            f"plans[{index}].analyzer_scope_comparison.reason",
        )
        task_list = require_list(plan_obj.get("task_list"), f"plans[{index}].task_list")
        if not task_list:
            raise ValidationError(f"plans[{index}].task_list must be non-empty")
        plan_task_indexes: list[str] = []
        expected_analyzer_scopes: set[str] = set()
        for task_index, item in enumerate(task_list):
            plan_task = require_dict(item, f"plans[{index}].task_list[{task_index}]")
            key = _validate_task_identity(
                plan_task,
                f"plans[{index}].task_list[{task_index}]",
            )
            if key in covered_tasks:
                raise ValidationError("task appears more than once in fix plan set")
            covered_tasks.add(key)
            plan_task_indexes.append(plan_task["task_index"])
            final_class = require_enum(
                plan_task.get("final_class"),
                f"plans[{index}].task_list[{task_index}].final_class",
                ENV_INFRA_CLASSES,
            )
            root_cause_code = require_string(
                plan_task.get("root_cause_code"),
                f"plans[{index}].task_list[{task_index}].root_cause_code",
            )
            if expected_tasks is not None:
                summary = expected_tasks.get(key)
                if summary is None:
                    raise ValidationError("fix plan contains a task absent from main agent input")
                _require_exact_task_identity(
                    plan_task,
                    summary["task"],
                    f"plans[{index}].task_list[{task_index}]",
                )
                alignment = summary["analyzer_alignment"]
                expected_analyzer_scopes.add(alignment["analyzer_scope"])
                if (
                    final_class != alignment["final_class"]
                    or root_cause_code != alignment["root_cause_code"]
                ):
                    raise ValidationError("fix plan task classification does not match task summary")
        if expected_tasks is not None:
            if set(analyzer_scopes) != expected_analyzer_scopes:
                raise ValidationError("fix plan analyzer scopes do not match task summaries")
            if relation != _scope_relation(fix_scope, expected_analyzer_scopes):
                raise ValidationError("fix plan scope relation does not match task summaries")
        actions = require_list(plan_obj.get("actions"), f"plans[{index}].actions")
        if not actions:
            raise ValidationError(f"plans[{index}].actions must be non-empty")
        action_ids: set[str] = set()
        for action_index, action in enumerate(actions):
            name = f"plans[{index}].actions[{action_index}]"
            action_id = _validate_fix_action(action, name)
            if action_id in action_ids:
                raise ValidationError(f"duplicate action_id in plan {plan_id}: {action_id}")
            action_ids.add(action_id)
        fix_reason = require_dict(plan_obj.get("fix_reason"), f"plans[{index}].fix_reason")
        require_string(fix_reason.get("summary"), f"plans[{index}].fix_reason.summary")
        require_list(fix_reason.get("evidence"), f"plans[{index}].fix_reason.evidence")
        require_string(fix_reason.get("reasoning"), f"plans[{index}].fix_reason.reasoning")
        verification = require_dict(
            plan_obj.get("verification_hint"),
            f"plans[{index}].verification_hint",
        )
        require_string(
            verification.get("expected_original_failure_absent"),
            f"plans[{index}].verification_hint.expected_original_failure_absent",
        )
        target_indexes = require_list(
            verification.get("target_task_indexes"),
            f"plans[{index}].verification_hint.target_task_indexes",
        )
        for target_index, value in enumerate(target_indexes):
            require_string(
                value,
                f"plans[{index}].verification_hint.target_task_indexes[{target_index}]",
            )
        if sorted(target_indexes) != sorted(plan_task_indexes):
            raise ValidationError(
                f"plans[{index}].verification_hint.target_task_indexes "
                "must match task_list"
            )
    for index, item in enumerate(
        require_list(payload.get("unplanned_tasks"), "unplanned_tasks")
    ):
        unplanned = require_dict(item, f"unplanned_tasks[{index}]")
        key = _validate_task_identity(unplanned, f"unplanned_tasks[{index}]")
        require_string(unplanned.get("reason"), f"unplanned_tasks[{index}].reason")
        if key in covered_tasks:
            raise ValidationError("task appears more than once in fix plan set")
        if expected_tasks is not None:
            summary = expected_tasks.get(key)
            if summary is None:
                raise ValidationError("unplanned task is absent from main agent input")
            _require_exact_task_identity(
                unplanned,
                summary["task"],
                f"unplanned_tasks[{index}]",
            )
        covered_tasks.add(key)
    require_list(payload.get("generation_errors"), "generation_errors")
    if expected_tasks is not None and covered_tasks != set(expected_tasks):
        raise ValidationError("fix plan set does not cover every main agent task summary")


def validate_exec_input(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_exec_input", name="exec input")
    require_string(payload.get("fix_plan_path"), "fix_plan_path")
    require_string(payload.get("workspace_root"), "workspace_root")
    validate_fix_plan_set(require_dict(payload.get("fix_plan"), "fix_plan"))


def _aggregate_exec_status(statuses: list[str]) -> str:
    failed_count = statuses.count("failed")
    if failed_count == 0:
        return "success"
    if failed_count == len(statuses):
        return "failed"
    return "partial_failed"


def validate_exec_result(payload: dict[str, Any]) -> None:
    _check_kind(payload, version=1, kind="harbor_fixer_exec_result", name="exec result")
    status = require_enum(payload.get("status"), "status", EXEC_STATUSES)
    policy_status = require_enum(
        payload.get("policy_status"), "policy_status", EXEC_POLICY_STATUSES
    )
    plans = require_list(payload.get("plans"), "plans")
    plan_statuses: list[str] = []
    for plan_index, plan in enumerate(plans):
        plan_obj = require_dict(plan, f"plans[{plan_index}]")
        plan_status = require_enum(
            plan_obj.get("status"),
            f"plans[{plan_index}].status",
            {"success", "failed"},
        )
        plan_statuses.append(plan_status)
        actions = require_list(
            plan_obj.get("actions"),
            f"plans[{plan_index}].actions",
        )
        for action_index, action in enumerate(actions):
            action_obj = require_dict(
                action,
                f"plans[{plan_index}].actions[{action_index}]",
            )
            action_status = require_enum(
                action_obj.get("status"),
                "action.status",
                EXEC_COMMAND_STATUSES,
            )
            exit_code = action_obj.get("exit_code")
            if action_status == "success" and exit_code != 0:
                raise ValidationError("successful action exit_code must be 0")
            if action_status == "skipped" and exit_code is not None:
                raise ValidationError("skipped action exit_code must be null")
    expected = _aggregate_exec_status(plan_statuses)
    if status != expected:
        raise ValidationError(f"status must be {expected}")
    if policy_status == "denied" and status != "failed":
        raise ValidationError("policy_status denied requires failed status")


def validate_verification_input(payload: dict[str, Any]) -> None:
    _check_kind(
        payload,
        version=2,
        kind="harbor_fixer_verification_input",
        name="verification input",
    )
    for key in (
        "fix_plan_path",
        "exec_result_path",
        "verification_run_dir",
        "output_dir",
    ):
        require_string(payload.get(key), key)
    for key in ("dataset_name", "dataset_path", "model"):
        if key in payload:
            require_string(payload.get(key), key, allow_empty=True)
    require_enum(payload.get("monitor_policy"), "monitor_policy", MONITOR_POLICIES)
    rerun_command = payload.get("rerun_command")
    if rerun_command is not None:
        rerun_command = require_string(
            rerun_command, "rerun_command", allow_empty=True
        )
        if rerun_command:
            try:
                argv = shlex.split(rerun_command)
            except ValueError as exc:
                raise ValidationError(f"rerun_command is invalid: {exc}") from None
            if not argv:
                raise ValidationError("rerun_command must not be blank")
    rerun_timeout = payload.get("rerun_timeout")
    if "rerun_timeout" in payload and (
        isinstance(rerun_timeout, bool)
        or not isinstance(rerun_timeout, int)
        or rerun_timeout <= 0
    ):
        raise ValidationError("rerun_timeout must be a positive integer")
    monitor_wait_timeout = payload.get("monitor_wait_timeout")
    if "monitor_wait_timeout" in payload and (
        isinstance(monitor_wait_timeout, bool)
        or not isinstance(monitor_wait_timeout, int)
        or monitor_wait_timeout <= 0
    ):
        raise ValidationError("monitor_wait_timeout must be a positive integer")
    monitor_poll_interval = payload.get("monitor_poll_interval")
    valid_poll_interval = "monitor_poll_interval" not in payload or (
        not isinstance(monitor_poll_interval, bool)
        and isinstance(monitor_poll_interval, (int, float))
        and monitor_poll_interval > 0
    )
    if valid_poll_interval and "monitor_poll_interval" in payload:
        try:
            valid_poll_interval = math.isfinite(float(monitor_poll_interval))
        except OverflowError:
            valid_poll_interval = False
    if not valid_poll_interval:
        raise ValidationError(
            "monitor_poll_interval must be a positive finite number"
        )
    if payload.get("verification_mode") != "smoke_test":
        raise ValidationError("verification_mode must be smoke_test")
    limit = payload.get("verification_task_limit_per_plan")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValidationError(
            "verification_task_limit_per_plan must be a positive integer"
        )
    fix_plan = require_dict(payload.get("fix_plan"), "fix_plan")
    exec_result = require_dict(payload.get("exec_result"), "exec_result")
    validate_fix_plan_set(fix_plan)
    validate_exec_result(exec_result)
    agent = require_enum(payload.get("agent"), "agent", HARBOR_AGENTS)
    fix_plan_source = require_dict(fix_plan.get("source"), "fix_plan.source")
    source_agent = require_string(
        fix_plan_source.get("agent", ""), "fix_plan.source.agent", allow_empty=True
    )
    if source_agent and agent != source_agent:
        raise ValidationError("agent does not match fix_plan source")
    exec_source = require_dict(exec_result.get("source"), "exec_result.source")
    fix_plan_digest = require_string(
        exec_source.get("fix_plan_sha256"),
        "exec_result.source.fix_plan_sha256",
    )
    if fix_plan_digest != json_sha256(fix_plan):
        raise ValidationError("exec_result does not match fix_plan")
    fix_plan_ids = {str(plan["plan_id"]) for plan in fix_plan["plans"]}
    exec_plan_ids = [
        require_string(plan.get("plan_id"), f"exec_result.plans[{index}].plan_id")
        for index, plan in enumerate(exec_result["plans"])
    ]
    if len(exec_plan_ids) != len(set(exec_plan_ids)):
        raise ValidationError("exec_result contains duplicate plan_id")
    if set(exec_plan_ids) != fix_plan_ids:
        raise ValidationError("exec_result plan_ids must match fix_plan")


def _validate_run_record(
    payload: dict[str, Any], name: str
) -> tuple[str, str]:
    task_index = require_string(payload.get("task_index"), f"{name}.task_index")
    task_name = require_string(payload.get("task_name"), f"{name}.task_name")
    require_enum(
        payload.get("task_complete_status"),
        f"{name}.task_complete_status",
        TASK_COMPLETE_STATUSES,
    )
    return task_index, task_name


def _validate_exec_failure_reason(
    payload: dict[str, Any], name: str, policy_status: str
) -> str:
    exec_status = require_enum(
        payload.get("exec_status"), f"{name}.exec_status", {"success", "failed"}
    )
    if policy_status == "denied" and exec_status != "failed":
        raise ValidationError(f"{name}.exec_status must be failed after policy denial")
    reason = payload.get("exec_failure_reason")
    if exec_status == "success":
        if reason is not None:
            raise ValidationError(
                f"{name}.exec_failure_reason must be null after successful execution"
            )
        return exec_status
    expected = "policy_denied" if policy_status == "denied" else "execution_failed"
    if reason != expected:
        raise ValidationError(f"{name}.exec_failure_reason must be {expected}")
    return exec_status


def _expected_verification_status(
    statuses: list[str], reason_codes: list[str]
) -> str:
    sampled = [status for status in statuses if status != "not_sampled"]
    if (
        not sampled
        or INCONCLUSIVE_VERIFICATION_REASONS.intersection(reason_codes)
    ):
        return "inconclusive"
    if "exec_failed" in sampled:
        return "exec_failed"
    if all(status == "fixed" for status in sampled):
        return "fixed"
    if "fixed" in sampled:
        return "partially_fixed"
    if any(status in {"unknown", "not_complete"} for status in sampled):
        return "inconclusive"
    return "not_fixed"


def validate_verification_result(payload: dict[str, Any]) -> None:
    _check_kind(
        payload,
        version=2,
        kind="harbor_fixer_verification_result",
        name="verification result",
    )
    require_enum(payload.get("agent"), "agent", HARBOR_AGENTS)
    status = require_enum(payload.get("status"), "status", VERIFICATION_STATUSES)
    require_dict(payload.get("source"), "source")
    execution = require_dict(payload.get("execution"), "execution")
    execution_status = require_enum(
        execution.get("status"), "execution.status", EXEC_STATUSES
    )
    policy_status = require_enum(
        execution.get("policy_status"),
        "execution.policy_status",
        EXEC_POLICY_STATUSES,
    )
    if policy_status == "denied" and execution_status != "failed":
        raise ValidationError("policy_status denied requires failed execution status")
    reason_codes = require_list(payload.get("reason_codes"), "reason_codes")
    for index, reason in enumerate(reason_codes):
        require_enum(
            reason,
            f"reason_codes[{index}]",
            VERIFICATION_REASON_CODES,
        )
    if len(reason_codes) != len(set(reason_codes)):
        raise ValidationError("reason_codes must not contain duplicates")
    exec_reason = "policy_denied" if policy_status == "denied" else "execution_failed"
    expected_exec_reasons = set() if execution_status == "success" else {exec_reason}
    if set(reason_codes).intersection(EXEC_FAILURE_REASONS) != expected_exec_reasons:
        raise ValidationError("reason_codes do not match execution status")
    require_dict(payload.get("rerun"), "rerun")
    require_dict(payload.get("new_run_summary"), "new_run_summary")
    if payload.get("verification_mode") != "smoke_test":
        raise ValidationError("verification_mode must be smoke_test")
    sampling = require_dict(payload.get("sampling"), "sampling")
    plan_task_count = sampling.get("plan_task_count")
    if (
        isinstance(plan_task_count, bool)
        or not isinstance(plan_task_count, int)
        or plan_task_count < 0
    ):
        raise ValidationError("sampling.plan_task_count must be a non-negative integer")
    if (plan_task_count == 0) != ("no_verifiable_tasks" in reason_codes):
        raise ValidationError(
            "no_verifiable_tasks must match sampling.plan_task_count"
        )
    plan_exec_statuses: list[str] = []
    for index, plan in enumerate(
        require_list(payload.get("plan_results"), "plan_results")
    ):
        plan_obj = require_dict(plan, f"plan_results[{index}]")
        require_string(plan_obj.get("plan_id"), f"plan_results[{index}].plan_id")
        plan_status = require_enum(
            plan_obj.get("status"),
            f"plan_results[{index}].status",
            VERIFICATION_STATUSES,
        )
        plan_exec_status = _validate_exec_failure_reason(
            plan_obj,
            f"plan_results[{index}]",
            policy_status,
        )
        plan_exec_statuses.append(plan_exec_status)
        if plan_exec_status == "failed" and plan_status != "exec_failed":
            raise ValidationError(
                f"plan_results[{index}].status must be exec_failed"
            )
        if plan_exec_status == "success" and plan_status == "exec_failed":
            raise ValidationError(
                f"plan_results[{index}].status cannot be exec_failed"
            )
    task_statuses: list[str] = []
    for index, task in enumerate(
        require_list(payload.get("task_results"), "task_results")
    ):
        task_obj = require_dict(task, f"task_results[{index}]")
        task_identity = require_dict(
            task_obj.get("task"),
            f"task_results[{index}].task",
        )
        _validate_task_identity(
            task_identity,
            f"task_results[{index}].task",
        )
        verification_status = require_enum(
            task_obj.get("verification_status"),
            f"task_results[{index}].verification_status",
            TASK_VERIFICATION_STATUSES,
        )
        task_statuses.append(verification_status)
        exec_status = _validate_exec_failure_reason(
            task_obj,
            f"task_results[{index}]",
            policy_status,
        )
        new_run = task_obj.get("new_run")
        if exec_status == "failed":
            expected_status = "exec_failed"
        elif new_run is None:
            expected_status = "not_sampled"
        else:
            run_record = require_dict(new_run, f"task_results[{index}].new_run")
            run_identity = _validate_run_record(
                run_record, f"task_results[{index}].new_run"
            )
            if run_identity != (
                task_identity["task_index"],
                task_identity["task_name"],
            ):
                raise ValidationError(
                    f"task_results[{index}].new_run identity does not match task"
                )
            expected_status = {
                "complete_success": "fixed",
                "complete_failed": "not_fixed",
                "complete_unknown": "unknown",
                "not_complete": "not_complete",
            }[run_record["task_complete_status"]]
        if verification_status != expected_status:
            raise ValidationError(
                f"task_results[{index}].verification_status must be "
                f"{expected_status}"
            )
        if exec_status == "failed" and new_run is not None:
            raise ValidationError(
                f"task_results[{index}].new_run must be null after failed execution"
            )
    if plan_exec_statuses:
        expected_execution_status = _aggregate_exec_status(plan_exec_statuses)
        if execution_status != expected_execution_status:
            raise ValidationError(
                f"execution.status must be {expected_execution_status}"
            )
    expected_status = _expected_verification_status(task_statuses, reason_codes)
    if status != expected_status:
        raise ValidationError(f"status must be {expected_status}")
    unexpected = require_list(
        payload.get("unexpected_run_task_results"),
        "unexpected_run_task_results",
    )
    for index, record in enumerate(unexpected):
        _validate_run_record(
            require_dict(record, f"unexpected_run_task_results[{index}]"),
            f"unexpected_run_task_results[{index}]",
        )
