"""Run per-task Pi Analyzer subagents and persist aggregate outputs."""

from __future__ import annotations

import json
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import PROMPT_VERSION, SCHEMA_VERSION, TAXONOMY_VERSION
from .contract import validate_handover
from .io import stable_hash, utc_now, write_json_atomic, write_text_atomic
from .pi import dispatch_to_child
from .prompt import build_dispatch_retry_prompt, build_task_prompt, build_validation_retry_prompt
from .taxonomy import ENV_INFRA_CLASSES, FINAL_CLASSES, UNKNOWN_ROOT_CAUSE
from .validation import validate_final_json, validate_task_analysis


AGENT_NAME = "harbor_analyzer_pi_subagent"
MAX_TASK_ATTEMPTS = 2
MAX_RETRY_TIMEOUT_SECONDS = 1800
RETRYABLE_BLOCK_REASONS = {
    "pi_dispatch_timeout",
    "pi_final_message_invalid_json",
    "pi_final_message_truncated",
}
RETRYABLE_BLOCK_REASON_PREFIXES = (
    "pi_provider_request_failed:",
)


@dataclass(frozen=True)
class AnalyzerConfig:
    run_dir: Path
    queue_dir: Path | None
    output_dir: Path
    run_id: str | None = None
    pi_bin: str = "pi"
    provider: str = "harbor-analyzer"
    model: str = ""
    base_url: str = ""
    api_key_env: str = "HARBOR_ANALYZER_API_KEY"
    timeout_seconds: int = 900
    max_concurrency: int = 1


def _analysis_id(
    *,
    prompt: str,
    provider: str,
    model: str,
    handover_id: str,
    task: dict[str, Any] | None = None,
    publication_id: str | None = None,
) -> str:
    digest = stable_hash(
        {
            "handover_id": handover_id,
            "publication_id": publication_id,
            "task": _task_identity(task) if task else None,
            "exact_pi_prompt": prompt,
            "provider": provider,
            "model": model,
            "agent_name": AGENT_NAME,
        }
    )
    return f"sha256-{digest}"


def _attempt_analysis_id(base_analysis_id: str, attempt: int) -> str:
    if attempt <= 1:
        return base_analysis_id
    return f"{base_analysis_id}-retry{attempt}"


def _publication_id(*, handover_id: str, run_id: Any) -> str:
    return f"sha256-{stable_hash({'handover_id': handover_id, 'run_id': run_id, 'nonce': uuid4().hex})}"


def _timeout_retry_seconds(timeout_seconds: int) -> int:
    return min(max(timeout_seconds * 2, timeout_seconds + 300), MAX_RETRY_TIMEOUT_SECONDS)


def _is_retryable_block_reason(reason: str) -> bool:
    return reason in RETRYABLE_BLOCK_REASONS or any(
        reason.startswith(prefix) for prefix in RETRYABLE_BLOCK_REASON_PREFIXES
    )


def _retry_timeout_seconds(reason: str, timeout_seconds: int) -> int:
    if reason == "pi_dispatch_timeout":
        return _timeout_retry_seconds(timeout_seconds)
    return timeout_seconds


def _task_identity(task: dict[str, Any] | None) -> dict[str, Any]:
    task = task or {}
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def _task_key(task: dict[str, Any] | None) -> tuple[str, str, str]:
    identity = _task_identity(task)
    return (
        identity["task_index"],
        identity["task_name"],
        "" if identity["attempt_id"] is None else str(identity["attempt_id"]),
    )


def _task_slug(task: dict[str, Any]) -> str:
    return f"task-{stable_hash(_task_identity(task))[:16]}"


def _is_contained_path(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _task_allowed_paths(task: dict[str, Any], *, handover_path: Path, config: AnalyzerConfig) -> list[Path | None]:
    paths: list[Path | None] = [handover_path, config.run_dir, config.queue_dir]
    evidence_roots = [config.run_dir.resolve()]
    if config.queue_dir is not None:
        evidence_roots.append(config.queue_dir.resolve())
    result_path = task.get("result_path")
    if isinstance(result_path, str) and result_path.strip():
        path = Path(result_path).expanduser()
        if not path.is_absolute():
            path = config.run_dir / path
        try:
            path = path.resolve()
        except OSError:
            return paths
        if _is_contained_path(path, evidence_roots):
            paths.append(path)
            if _is_contained_path(path.parent, evidence_roots):
                paths.append(path.parent)
    return paths


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)


@contextmanager
def _publish_lock(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".analyzer-publish.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _task_report_locations(benchmark_report: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    tasks = benchmark_report.get("tasks")
    if not isinstance(tasks, list):
        return {}

    lines = json.dumps(benchmark_report, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    try:
        tasks_line_index = next(index for index, line in enumerate(lines) if line == '  "tasks": [')
    except StopIteration:
        return {}

    ranges: list[tuple[int, int]] = []
    index = tasks_line_index + 1
    while index < len(lines):
        line = lines[index]
        if line in {"  ]", "  ],"}:
            break
        if line == "    {":
            start = index + 1
            end = start
            for end_index in range(index + 1, len(lines)):
                if lines[end_index] in {"    }", "    },"}:
                    end = end_index + 1
                    index = end_index
                    break
            ranges.append((start, end))
        index += 1

    locations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for task_index, task_report in enumerate(tasks):
        if not isinstance(task_report, dict) or task_index >= len(ranges):
            continue
        task = task_report.get("task")
        if not isinstance(task, dict):
            continue
        line_start, line_end = ranges[task_index]
        locations[_task_key(task)] = {
            "analysis_report_pointer": f"/tasks/{task_index}",
            "analysis_report_line_start": line_start,
            "analysis_report_line_end": line_end,
        }
    return locations


def _enrich_fix_line_index(
    records: list[dict[str, Any]],
    *,
    benchmark_report: dict[str, Any],
    raw_task_dir: Path,
    report_path: Path,
) -> list[dict[str, Any]]:
    locations = _task_report_locations(benchmark_report)
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["analysis_report_path"] = str(report_path)
        item["analysis_report_snapshot_path"] = str(report_path)
        task = item.get("task")
        if isinstance(task, dict):
            item["task_analysis_path"] = str(raw_task_dir / f"{_task_slug(task)}.json")
            location = locations.get(_task_key(task))
            if location:
                item.update(location)
        enriched.append(item)
    return enriched


def _empty_final_json(*, handover_id: str, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "harbor_analyzer_final",
        "handover_id": handover_id,
        "run_id": run_id,
        "benchmark_report": {
            "schema_version": SCHEMA_VERSION,
            "kind": "harbor_benchmark_root_cause_report",
            "handover_id": handover_id,
            "run_id": run_id,
            "generated_at": utc_now(),
            "summary": {
                "task_count": 0,
                "final_class_counts": {},
                "benchmark_level_summary": "No handover task required analyzer work.",
            },
            "tasks": [],
        },
        "env_infra_tasks": {
            "schema_version": SCHEMA_VERSION,
            "kind": "harbor_env_infra_task_list",
            "handover_id": handover_id,
            "generated_at": utc_now(),
            "task_count": 0,
            "tasks": [],
        },
        "fix_line_index": [],
    }


def _failed_task_analysis(
    *,
    handover_id: str,
    run_id: str,
    task: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "harbor_task_root_cause_analysis",
        "handover_id": handover_id,
        "run_id": run_id,
        "task": _task_identity(task),
        "analysis_status": "analysis_failed",
        "final_class": "unknown",
        "failure_stage": "unknown",
        "root_cause_code": UNKNOWN_ROOT_CAUSE,
        "root_cause_summary": f"Analyzer subagent did not return a valid analysis: {reason}",
        "scope": "task",
        "confidence": 0.0,
        "observations": [],
        "reasoning_summary": "This task needs analyzer rerun or manual inspection because the per-task subagent result was unavailable or invalid.",
        "alternatives_considered": [],
        "recommended_events": ["notify_user"],
        "fix_references": [],
    }


def _repair_task_analysis(task_json: dict[str, Any]) -> dict[str, Any]:
    """Fill safe mechanical defaults before schema validation.

    This intentionally does not infer semantic root cause fields.  It only fixes
    optional-array style omissions that do not change a task classification.
    """

    repaired = dict(task_json)
    repaired.pop("recommended_actions", None)
    repaired.pop("fix_goal", None)
    repairs: list[str] = []
    final_class = repaired.get("final_class")

    if repaired.get("alternatives_considered") is None:
        repaired["alternatives_considered"] = []
        repairs.append("alternatives_considered_empty_array")

    if repaired.get("recommended_events") != ["notify_user"]:
        repaired["recommended_events"] = ["notify_user"]
        repairs.append("recommended_events_notify_user_only")

    if final_class in {"success", "model_fail", "unknown"} and repaired.get("fix_references") is None:
        repaired["fix_references"] = []
        repairs.append("fix_references_empty_array")

    if repairs:
        existing = repaired.get("schema_repairs")
        if isinstance(existing, list):
            repaired["schema_repairs"] = existing + repairs
        else:
            repaired["schema_repairs"] = repairs
    return repaired


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _assemble_final_json(
    *,
    handover_id: str,
    run_id: str,
    task_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    final_class_counts = _counts(
        [
            str(report.get("final_class"))
            for report in task_reports
            if report.get("final_class") in FINAL_CLASSES
        ]
    )
    status_counts = _counts(
        [
            str(report.get("analysis_status"))
            for report in task_reports
            if isinstance(report.get("analysis_status"), str)
        ]
    )
    env_reports = [
        report
        for report in task_reports
        if report.get("final_class") in ENV_INFRA_CLASSES
    ]

    env_tasks: list[dict[str, Any]] = []
    fix_line_index: list[dict[str, Any]] = []
    for report in env_reports:
        env_tasks.append(
            {
                "task": report.get("task"),
                "final_class": report.get("final_class"),
                "failure_stage": report.get("failure_stage"),
                "scope": report.get("scope"),
                "confidence": report.get("confidence"),
                "root_cause_code": report.get("root_cause_code"),
                "root_cause_summary": report.get("root_cause_summary"),
            }
        )
        references = report.get("fix_references")
        if not isinstance(references, list):
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            record = {
                "schema_version": SCHEMA_VERSION,
                "kind": "harbor_fix_line_reference",
                "task": report.get("task"),
                "root_cause_code": report.get("root_cause_code"),
                "path": reference.get("path"),
                "line_start": reference.get("line_start"),
                "line_end": reference.get("line_end"),
                "fact": reference.get("fact"),
                "reason": reference.get("reason"),
            }
            snippet = reference.get("snippet")
            if isinstance(snippet, list):
                record["snippet"] = snippet
            fix_line_index.append(record)

    generated_at = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "harbor_analyzer_final",
        "handover_id": handover_id,
        "run_id": run_id,
        "benchmark_report": {
            "schema_version": SCHEMA_VERSION,
            "kind": "harbor_benchmark_root_cause_report",
            "handover_id": handover_id,
            "run_id": run_id,
            "generated_at": generated_at,
            "summary": {
                "task_count": len(task_reports),
                "analysis_status_counts": status_counts,
                "final_class_counts": final_class_counts,
                "benchmark_level_summary": (
                    f"Analyzed {len(task_reports)} task(s); "
                    f"{len(env_reports)} task(s) were classified as env/infra."
                ),
            },
            "tasks": task_reports,
        },
        "env_infra_tasks": {
            "schema_version": SCHEMA_VERSION,
            "kind": "harbor_env_infra_task_list",
            "handover_id": handover_id,
            "generated_at": generated_at,
            "task_count": len(env_tasks),
            "tasks": env_tasks,
        },
        "fix_line_index": fix_line_index,
    }


def _write_error(
    *,
    config: AnalyzerConfig,
    handover_id: str,
    analysis_id: str,
    reason: str,
    provenance: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "harbor_analyzer_error",
        "handover_id": handover_id,
        "analysis_id": analysis_id,
        "generated_at": utc_now(),
        "reason": reason,
        "validation_errors": validation_errors or [],
        "agent_provenance": provenance or {},
    }
    write_json_atomic(config.output_dir / "analyzer-error-latest.json", payload)
    write_json_atomic(config.output_dir / "analyzer-errors" / f"{handover_id}.json", payload)
    return payload


def _write_outputs(
    *,
    config: AnalyzerConfig,
    handover_id: str,
    publication_id: str | None = None,
    final_json: dict[str, Any],
    provenance: dict[str, Any] | None,
    prompt_path: Path | None,
    raw_final_json_path: Path | None,
) -> dict[str, Any]:
    benchmark_report = dict(final_json.get("benchmark_report") or {})
    env_infra_tasks = dict(final_json.get("env_infra_tasks") or {})
    fix_line_index = final_json.get("fix_line_index")
    if not isinstance(fix_line_index, list):
        fix_line_index = []

    publication_id = publication_id or _publication_id(
        handover_id=handover_id,
        run_id=benchmark_report.get("run_id") or final_json.get("run_id"),
    )
    report_path = config.output_dir / "analyzer-runs" / handover_id / f"{publication_id}.json"
    env_infra_path = config.output_dir / "env-infra-tasks" / handover_id / f"{publication_id}.json"
    fix_index_path = config.output_dir / "fix-line-index" / handover_id / f"{publication_id}.jsonl"
    latest_artifacts_path = config.output_dir / "analyzer-artifacts-latest.json"
    raw_final_snapshot_path = (
        raw_final_json_path.parent / handover_id / f"{publication_id}.json"
        if raw_final_json_path
        else None
    )

    benchmark_report.setdefault("schema_version", SCHEMA_VERSION)
    benchmark_report.setdefault("kind", "harbor_benchmark_root_cause_report")
    benchmark_report["analyzer_metadata"] = {
        "prompt_version": PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "prompt_path": str(prompt_path) if prompt_path else None,
        "publication_id": publication_id,
        "raw_final_json_path": str(raw_final_snapshot_path) if raw_final_snapshot_path else None,
        "env_infra_tasks_path": str(env_infra_path),
        "fix_line_index_path": str(fix_index_path),
        "latest_artifacts_path": str(latest_artifacts_path),
        "agent_provenance": provenance or {},
    }

    env_infra_tasks.setdefault("schema_version", SCHEMA_VERSION)
    env_infra_tasks.setdefault("kind", "harbor_env_infra_task_list")
    env_infra_tasks["source_report_path"] = str(report_path)
    env_infra_tasks["fix_line_index_path"] = str(fix_index_path)
    env_infra_tasks["generated_at"] = env_infra_tasks.get("generated_at") or utc_now()
    tasks = env_infra_tasks.get("tasks")
    env_infra_tasks["task_count"] = len(tasks) if isinstance(tasks, list) else 0

    fix_line_index = _enrich_fix_line_index(
        fix_line_index,
        benchmark_report=benchmark_report,
        raw_task_dir=config.output_dir / "analyzer-task-json" / handover_id / publication_id,
        report_path=report_path,
    )
    final_json["benchmark_report"] = benchmark_report
    final_json["env_infra_tasks"] = env_infra_tasks
    final_json["fix_line_index"] = fix_line_index

    fix_index_jsonl = _jsonl(fix_line_index)
    write_json_atomic(report_path, benchmark_report)
    write_json_atomic(env_infra_path, env_infra_tasks)
    write_text_atomic(fix_index_path, fix_index_jsonl)
    if raw_final_snapshot_path:
        write_json_atomic(raw_final_snapshot_path, final_json)
    latest_artifacts = {
        "schema_version": SCHEMA_VERSION,
        "kind": "harbor_analyzer_latest_artifacts",
        "handover_id": handover_id,
        "publication_id": publication_id,
        "run_id": benchmark_report.get("run_id"),
        "generated_at": utc_now(),
        "artifacts": {
            "benchmark_report_path": str(report_path),
            "env_infra_tasks_path": str(env_infra_path),
            "fix_line_index_path": str(fix_index_path),
            "raw_final_json_path": str(raw_final_snapshot_path) if raw_final_snapshot_path else None,
        },
    }
    with _publish_lock(config.output_dir):
        write_json_atomic(latest_artifacts_path, latest_artifacts)
    return benchmark_report


def _run_task_analysis(
    *,
    task: dict[str, Any],
    handover_path: Path,
    config: AnalyzerConfig,
    run_id: str,
    handover_id: str,
    publication_id: str,
    prompt_dir: Path,
    raw_task_dir: Path,
) -> dict[str, Any]:
    task_slug = _task_slug(task)
    base_prompt = build_task_prompt(
        agent_name=AGENT_NAME,
        handover_path=handover_path.resolve(),
        run_dir=config.run_dir.resolve(),
        queue_dir=config.queue_dir.resolve() if config.queue_dir else None,
        run_id=run_id,
        handover_id=handover_id,
        task=task,
    )
    task_analysis_id = _analysis_id(
        prompt=base_prompt,
        provider=config.provider,
        model=config.model,
        handover_id=handover_id,
        task=task,
        publication_id=publication_id,
    )
    prompt = base_prompt
    base_prompt_path = prompt_dir / f"{task_slug}.txt"
    write_text_atomic(base_prompt_path, base_prompt)
    attempt_provenance: list[dict[str, Any]] = []
    attempt = 1
    timeout_seconds = config.timeout_seconds
    task_done = False
    task_report: dict[str, Any] | None = None
    task_error: dict[str, Any] | None = None

    while attempt <= MAX_TASK_ATTEMPTS:
        attempt_analysis_id = _attempt_analysis_id(task_analysis_id, attempt)
        prompt_path = base_prompt_path
        if attempt > 1:
            prompt_path = prompt_dir / f"{task_slug}.attempt{attempt}.txt"
            write_text_atomic(prompt_path, prompt)
        dispatched = dispatch_to_child(
            prompt=prompt,
            analysis_id=attempt_analysis_id,
            output_dir=config.output_dir / "analyzer-task-evidence" / handover_id / publication_id,
            pi_bin=config.pi_bin,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            agent_name=AGENT_NAME,
            timeout_seconds=timeout_seconds,
            allowed_paths=_task_allowed_paths(task, handover_path=handover_path, config=config),
        )
        provenance = dict(dispatched.provenance)
        provenance["analysis_id"] = attempt_analysis_id
        provenance["base_analysis_id"] = task_analysis_id
        provenance["attempt"] = attempt
        provenance["prompt_path"] = str(prompt_path)
        provenance["timeout_seconds"] = timeout_seconds
        attempt_provenance.append(provenance)

        reason = dispatched.block_reason or "child_final_json_missing"
        if dispatched.block_reason or dispatched.report is None:
            provenance["block_reason"] = reason
            if _is_retryable_block_reason(reason) and attempt < MAX_TASK_ATTEMPTS:
                provenance["retry_decision"] = "retry"
                provenance["retry_reason"] = reason
                timeout_seconds = _retry_timeout_seconds(reason, timeout_seconds)
                prompt = build_dispatch_retry_prompt(base_prompt=base_prompt, block_reason=reason)
                attempt += 1
                continue
            provenance["retry_decision"] = "fallback"
            task_error = {
                "task": _task_identity(task),
                "analysis_id": task_analysis_id,
                "final_attempt_analysis_id": attempt_analysis_id,
                "reason": reason,
                "validation_errors": [],
                "attempts": attempt_provenance,
            }
            task_report = _failed_task_analysis(
                handover_id=handover_id,
                run_id=run_id,
                task=task,
                reason=reason,
            )
            task_done = True
            break

        task_report = _repair_task_analysis(dispatched.report)
        repairs = task_report.get("schema_repairs")
        if isinstance(repairs, list) and repairs:
            provenance["schema_repairs"] = repairs
        raw_task_attempt_path = raw_task_dir / f"{task_slug}.attempt{attempt}.json"
        write_json_atomic(raw_task_attempt_path, task_report)
        validation_errors = validate_task_analysis(
            task_report,
            handover_id=handover_id,
            expected_task=task,
            tool_access_audit_path=provenance.get("tool_access_audit_path"),
        )
        provenance["raw_task_json_path"] = str(raw_task_attempt_path)
        if validation_errors:
            provenance["validation_errors"] = validation_errors
            if attempt < MAX_TASK_ATTEMPTS:
                prompt = build_validation_retry_prompt(
                    base_prompt=base_prompt,
                    previous_json=task_report,
                    validation_errors=validation_errors,
                )
                attempt += 1
                continue
            raw_task_path = raw_task_dir / f"{task_slug}.json"
            write_json_atomic(raw_task_path, task_report)
            task_error = {
                "task": _task_identity(task),
                "analysis_id": task_analysis_id,
                "final_attempt_analysis_id": attempt_analysis_id,
                "reason": "child_task_json_validation_failed",
                "validation_errors": validation_errors,
                "raw_task_json_path": str(raw_task_path),
                "attempts": attempt_provenance,
            }
            task_report = _failed_task_analysis(
                handover_id=handover_id,
                run_id=run_id,
                task=task,
                reason="child_task_json_validation_failed",
            )
            task_done = True
            break

        raw_task_path = raw_task_dir / f"{task_slug}.json"
        write_json_atomic(raw_task_path, task_report)
        task_done = True
        break

    if not task_done:
        task_error = {
            "task": _task_identity(task),
            "analysis_id": task_analysis_id,
            "reason": "child_task_attempts_exhausted",
            "validation_errors": [],
            "attempts": attempt_provenance,
        }
        task_report = _failed_task_analysis(
            handover_id=handover_id,
            run_id=run_id,
            task=task,
            reason="child_task_attempts_exhausted",
        )

    final_provenance = None
    if attempt_provenance:
        final_provenance = dict(attempt_provenance[-1])
        final_provenance["attempts"] = attempt_provenance
        final_provenance["attempt_count"] = len(attempt_provenance)

    return {
        "task_slug": task_slug,
        "task_report": task_report,
        "task_error": task_error,
        "provenance": final_provenance,
    }


def run_handover(
    handover: dict[str, Any],
    *,
    handover_path: Path,
    config: AnalyzerConfig,
) -> tuple[dict[str, Any], int]:
    tasks = validate_handover(
        handover,
        run_dir=config.run_dir,
        queue_dir=config.queue_dir,
    )
    handover_id = str(handover["handover_id"])
    run_id = config.run_id or str(handover.get("run_id") or config.run_dir.name)
    analysis_id = f"sha256-{stable_hash({'handover_id': handover_id, 'run_id': run_id, 'dispatch_mode': 'per_task_pi', 'task_count': len(tasks), 'provider': config.provider, 'model': config.model})}"
    publication_id = _publication_id(handover_id=handover_id, run_id=run_id)

    if not tasks:
        final_json = _empty_final_json(handover_id=handover_id, run_id=run_id)
        aggregate = _write_outputs(
            config=config,
            handover_id=handover_id,
            publication_id=publication_id,
            final_json=final_json,
            provenance={"analysis_id": analysis_id, "subagent_skipped": True},
            prompt_path=None,
            raw_final_json_path=None,
        )
        return aggregate, 0

    prompt_dir = config.output_dir / "analyzer-prompts" / handover_id / publication_id
    raw_task_dir = config.output_dir / "analyzer-task-json" / handover_id / publication_id
    task_reports: list[dict[str, Any]] = []
    task_errors: list[dict[str, Any]] = []
    per_task_provenance: dict[str, dict[str, Any]] = {}

    task_results: list[dict[str, Any] | None] = [None] * len(tasks)
    if config.max_concurrency == 1 or len(tasks) == 1:
        for index, task in enumerate(tasks):
            task_results[index] = _run_task_analysis(
                task=task,
                handover_path=handover_path,
                config=config,
                run_id=run_id,
                handover_id=handover_id,
                publication_id=publication_id,
                prompt_dir=prompt_dir,
                raw_task_dir=raw_task_dir,
            )
    else:
        max_workers = min(config.max_concurrency, len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(
                    _run_task_analysis,
                    task=task,
                    handover_path=handover_path,
                    config=config,
                    run_id=run_id,
                    handover_id=handover_id,
                    publication_id=publication_id,
                    prompt_dir=prompt_dir,
                    raw_task_dir=raw_task_dir,
                ): index
                for index, task in enumerate(tasks)
            }
            for future in as_completed(future_to_index):
                task_results[future_to_index[future]] = future.result()

    for result in task_results:
        if result is None:
            continue
        task_reports.append(result["task_report"])
        if result.get("task_error"):
            task_errors.append(result["task_error"])
        if result.get("provenance"):
            per_task_provenance[str(result["task_slug"])] = result["provenance"]

    final_json = _assemble_final_json(
        handover_id=handover_id,
        run_id=run_id,
        task_reports=task_reports,
    )
    raw_final_json_path = config.output_dir / "analyzer-final-json" / f"{handover_id}.json"
    validation_errors = validate_final_json(
        final_json,
        handover_id=handover_id,
        handover_tasks=tasks,
    )
    aggregate_provenance = {
        "analysis_id": analysis_id,
        "dispatch_mode": "per_task_pi_subagent",
        "max_concurrency": config.max_concurrency,
        "task_error_count": len(task_errors),
        "task_errors": task_errors,
        "per_task": per_task_provenance,
    }
    if validation_errors:
        error = _write_error(
            config=config,
            handover_id=handover_id,
            analysis_id=analysis_id,
            reason="child_final_json_validation_failed",
            provenance=aggregate_provenance,
            validation_errors=validation_errors,
        )
        return error, 2

    if task_errors:
        write_json_atomic(
            config.output_dir / "analyzer-task-errors" / f"{handover_id}.json",
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "harbor_analyzer_task_errors",
                "handover_id": handover_id,
                "analysis_id": analysis_id,
                "generated_at": utc_now(),
                "errors": task_errors,
            },
        )

    aggregate = _write_outputs(
        config=config,
        handover_id=handover_id,
        publication_id=publication_id,
        final_json=final_json,
        provenance=aggregate_provenance,
        prompt_path=None,
        raw_final_json_path=raw_final_json_path,
    )
    return aggregate, 3 if task_errors else 0
