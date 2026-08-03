"""Lightweight validation helpers for Harbor Fixer MVP artifacts."""

from __future__ import annotations

import json
import math
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


def _check_kind_versions(payload: dict[str, Any], *, versions: set[int], kind: str, name: str) -> int:
    require_dict(payload, name)
    version = payload.get("schema_version")
    if version not in versions:
        raise ValidationError(f"{name} schema_version must be one of: {', '.join(str(item) for item in sorted(versions))}")
    if payload.get("kind") != kind:
        raise ValidationError(f"{name} kind must be {kind}")
    return int(version)


def validate_analyzer_manifest(payload: dict[str, Any]) -> None:
    _check_kind(
        payload,
        version=2,
        kind="harbor_analyzer_latest_artifacts",
        name="analyzer artifact manifest",
    )
    require_string(payload.get("run_id"), "analyzer artifact manifest run_id")
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
    _check_kind(payload, version=1, kind="harbor_fixer_fix_plan_set", name="fix plan set")
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
        commands = require_list(plan_obj.get("commands"), f"plans[{index}].commands")
        if not commands:
            raise ValidationError(f"plans[{index}].commands must be non-empty")
        command_ids: set[str] = set()
        for command_index, command in enumerate(commands):
            command_obj = require_dict(command, f"plans[{index}].commands[{command_index}]")
            command_id = require_string(
                command_obj.get("command_id"),
                f"plans[{index}].commands[{command_index}].command_id",
            )
            if command_id in command_ids:
                raise ValidationError(f"duplicate command_id in plan {plan_id}: {command_id}")
            command_ids.add(command_id)
            require_string(command_obj.get("cwd"), f"plans[{index}].commands[{command_index}].cwd")
            require_string(command_obj.get("command"), f"plans[{index}].commands[{command_index}].command")
            require_string(
                command_obj.get("purpose"),
                f"plans[{index}].commands[{command_index}].purpose",
            )
            require_string(
                command_obj.get("expected_effect"),
                f"plans[{index}].commands[{command_index}].expected_effect",
            )
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
