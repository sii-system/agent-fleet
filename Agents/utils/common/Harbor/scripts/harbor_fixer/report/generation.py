"""Machine-readable Harbor Fixer report generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent_invocation import AgentInvoker
from ..analyzer_inputs import resolve_analyzer_paths
from ..artifact_io import read_json, write_json_atomic
from ..validation import (
    TASK_COMPLETE_STATUSES,
    ValidationError,
    json_sha256,
    report_rerun_facts,
    task_key,
    validate_env_infra_tasks,
    validate_exec_result,
    validate_fix_plan_set,
    validate_fix_report,
    validate_verification_result,
)
from ..verification.run_state import (
    collect_task_results,
    generate_monitor_snapshot,
    read_monitor_snapshot,
)
from .runtime import generate_report_summary


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _prepare_report_output(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "fix-report-latest.json").unlink(missing_ok=True)
        (output_dir / "fix-report-latest.md").unlink(missing_ok=True)
    except OSError as exc:
        raise ValidationError(f"cannot prepare report output: {exc}") from None


def _task_identity(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": str(task.get("task_index") or ""),
        "task_name": str(task.get("task_name") or ""),
        "attempt_id": task.get("attempt_id"),
    }


def _explicit_status(env_task: dict[str, Any]) -> tuple[str, str]:
    for source, value in (
        ("env_infra_task.task_complete_status", env_task.get("task_complete_status")),
        ("env_infra_task.complete_status", env_task.get("complete_status")),
        ("env_infra_task.status", env_task.get("status")),
    ):
        if isinstance(value, str) and value in TASK_COMPLETE_STATUSES:
            return value, source
    return "", "unavailable"


def _monitor_snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    return {
        "benchmark_status": str(snapshot.get("benchmark_status") or ""),
        "status_reason": str(snapshot.get("status_reason") or ""),
        "task_summary": snapshot.get("task_summary")
        if isinstance(snapshot.get("task_summary"), dict)
        else {},
    }


def _old_run_monitor_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "old_run_monitor_status": str(record.get("task_complete_status") or "")
        if record
        else "",
        "old_run_monitor": record or {},
    }


def _old_run_status_fields(old_task: dict[str, Any]) -> dict[str, Any]:
    return {
        "old_run_status": str(old_task.get("old_run_status") or ""),
        "old_run_status_source": str(old_task.get("old_run_status_source") or ""),
        "old_run_monitor_status": str(old_task.get("old_run_monitor_status") or ""),
        "old_run_monitor": old_task.get("old_run_monitor")
        if isinstance(old_task.get("old_run_monitor"), dict)
        else {},
    }


def _analyzer_facts(env_task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(env_task.get(key) or "")
        for key in (
            "final_class",
            "failure_stage",
            "scope",
            "root_cause_code",
            "root_cause_summary",
        )
    } | {"confidence": env_task.get("confidence")}


def _summary_task_result(item: dict[str, Any]) -> dict[str, Any]:
    new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
    return {
        "task": item.get("task", {}),
        "plan_id": item.get("plan_id", ""),
        "sampled": item.get("sampled"),
        "old_run_status": item.get("old_run_status", ""),
        "old_run_monitor_status": item.get("old_run_monitor_status", ""),
        "exec_status": item.get("exec_status", ""),
        "exec_failure_reason": item.get("exec_failure_reason"),
        "new_run_status": new_run.get("task_complete_status", ""),
        "verification_status": item.get("verification_status", ""),
    }


def _summary_caveats(
    verification_result: dict[str, Any],
    *,
    baseline_monitor_required: bool,
    baseline_monitor_available: bool,
) -> list[str]:
    caveats: list[str] = []
    if not baseline_monitor_available:
        caveats.append(
            "baseline monitor data unavailable; before/after comparison omitted"
        )
        if baseline_monitor_required:
            caveats.append("baseline monitor data was required by policy")
    if verification_result.get("status") in {"inconclusive", "exec_failed"}:
        caveats.append(f"verification status is {verification_result.get('status')}")
    if verification_result.get("verification_mode") == "smoke_test":
        sampling = verification_result.get("sampling", {})
        caveats.append(
            "verification used smoke sampling: "
            f"{sampling.get('sampled_task_count', 0)} sampled task(s), "
            f"{sampling.get('unsampled_task_count', 0)} unsampled task(s)"
        )
    return caveats


def _collect_baseline_monitor(
    baseline_run_dir: Path | None,
    output_dir: Path,
    baseline_monitor_policy: str,
    agent: str,
) -> tuple[
    dict[str, Any] | None, str, dict[str, dict[str, Any]], dict[str, Any] | None
]:
    if baseline_run_dir is None:
        return None, "", {}, None
    if baseline_monitor_policy == "off":
        return None, "", {}, None
    snapshot = None
    output_path = ""
    snapshot, output_path = read_monitor_snapshot(baseline_run_dir)
    if snapshot is None:
        snapshot, output_path = generate_monitor_snapshot(
            baseline_run_dir,
            output_dir / "baseline-monitor",
            agent,
        )
    records, summary = collect_task_results(baseline_run_dir, agent)
    return snapshot, output_path, records, summary


def _build_old_run(
    analyzer_paths: dict[str, Any],
    output_dir: Path,
    *,
    baseline_run_dir: Path | None,
    baseline_monitor_policy: str,
    agent: str,
) -> tuple[dict[str, Any], dict[tuple[str, str, str], dict[str, Any]], str]:
    snapshot, monitor_output_path, monitor_records, monitor_summary = (
        _collect_baseline_monitor(
            baseline_run_dir,
            output_dir,
            baseline_monitor_policy,
            agent,
        )
    )
    if snapshot is not None:
        handover = snapshot.get("analyzer_handover")
        if not isinstance(handover, dict) or (
            handover.get("run_id") != analyzer_paths["run_id"]
            or handover.get("agent") != agent
        ):
            raise ValidationError("baseline monitor does not match analyzer run")
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    monitor_task_keys: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    handover_ids: list[str] = []
    for publication in analyzer_paths["publications"]:
        env_infra = read_json(Path(publication["env_infra_tasks_path"]))
        validate_env_infra_tasks(env_infra)
        if env_infra.get("handover_id") != publication["handover_id"]:
            raise ValidationError("env/infra handover_id does not match analyzer manifest")
        handover_ids.append(str(env_infra.get("handover_id") or ""))
        for item in env_infra.get("tasks", []):
            if not isinstance(item, dict) or not isinstance(item.get("task"), dict):
                continue
            task = item["task"]
            task_index = str(task.get("task_index") or "")
            task_name = str(task.get("task_name") or "")
            key = task_key(task)
            monitor_task_keys.setdefault((task_index, task_name), set()).add(key)
            status, source = _explicit_status(item)
            by_key[key] = {
                "task": _task_identity(task),
                "old_run_status": status,
                "old_run_status_source": source,
                **_old_run_monitor_fields(None),
                "analyzer": _analyzer_facts(item),
            }
    for (task_index, task_name), keys in monitor_task_keys.items():
        monitor_record = monitor_records.get(task_index)
        if (
            len(keys) == 1
            and monitor_record
            and monitor_record.get("task_name") == task_name
        ):
            by_key[next(iter(keys))].update(_old_run_monitor_fields(monitor_record))
    old_tasks = list(by_key.values())

    old_run = {
        "run_id": str(analyzer_paths.get("run_id") or ""),
        "handover_id": handover_ids[-1] if handover_ids else "",
        "handover_ids": handover_ids,
        "analyzer_summary": {
            "task_count": len(old_tasks),
            "publication_count": len(analyzer_paths["publications"]),
        },
        "env_infra_task_count": len(old_tasks),
        "tasks": old_tasks,
        "monitor_available": snapshot is not None and bool(monitor_records),
        "monitor_summary": monitor_summary or {},
        "monitor_snapshot": _monitor_snapshot_summary(snapshot),
    }
    return old_run, by_key, monitor_output_path


def _with_old_status(
    record: dict[str, Any],
    old_task_by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    return {**record, **_old_run_status_fields(old_task_by_key.get(task_key(task), {}))}


def _summary_input(
    status: str,
    old_run: dict[str, Any],
    verification_result: dict[str, Any],
    task_results: list[dict[str, Any]],
    *,
    baseline_monitor_required: bool,
    baseline_monitor_available: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_report_summary_input",
        "status": status,
        "agent": verification_result.get("agent", ""),
        "execution": verification_result.get("execution", {}),
        "reason_codes": verification_result.get("reason_codes", []),
        "old_run": {
            "run_id": old_run.get("run_id", ""),
            "handover_id": old_run.get("handover_id", ""),
            "analyzer_summary": old_run.get("analyzer_summary", {}),
            "env_infra_task_count": old_run.get("env_infra_task_count", 0),
            "monitor_available": old_run.get("monitor_available", False),
            "monitor_summary": old_run.get("monitor_summary", {}),
        },
        "new_run": {
            "summary": verification_result.get("new_run_summary", {}),
            "rerun": report_rerun_facts(verification_result.get("rerun")),
            "verification_mode": verification_result.get(
                "verification_mode", "full_run"
            ),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result.get("plan_results", []),
        "task_results": [_summary_task_result(item) for item in task_results],
        "caveats": _summary_caveats(
            verification_result,
            baseline_monitor_required=baseline_monitor_required,
            baseline_monitor_available=baseline_monitor_available,
        ),
    }


def _report_source(
    verification_result_path: Path,
    analyzer_paths: dict[str, Any],
    baseline_run_dir: Path | None,
    baseline_monitor_output_path: str,
) -> dict[str, Any]:
    return {
        "verification_result_path": str(verification_result_path),
        "analyzer_output_path": str(analyzer_paths["analyzer_root"]),
        "analyzer_manifest_path": str(analyzer_paths["manifest_path"]),
        "env_infra_tasks_paths": [
            str(item["env_infra_tasks_path"]) for item in analyzer_paths["publications"]
        ],
        "baseline_run_dir": str(baseline_run_dir)
        if baseline_run_dir is not None
        else "",
        "baseline_monitor_output_path": baseline_monitor_output_path,
    }


def _source_artifact_path(
    verification_result: dict[str, Any], field: str
) -> Path:
    source = verification_result.get("source")
    value = source.get(field) if isinstance(source, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"verification source.{field} must be a non-empty string"
        )
    path = Path(value).expanduser()
    return path if path.is_absolute() else path.resolve()


def _fix_plan_path(verification_result: dict[str, Any]) -> Path:
    return _source_artifact_path(verification_result, "fix_plan_path")


def _validate_analyzer_provenance(
    verification_result: dict[str, Any],
    analyzer_paths: dict[str, Any],
) -> None:
    fix_plan = read_json(_fix_plan_path(verification_result))
    validate_fix_plan_set(fix_plan)
    exec_result = read_json(
        _source_artifact_path(verification_result, "exec_result_path")
    )
    validate_exec_result(exec_result)
    if exec_result["source"].get("fix_plan_sha256") != json_sha256(fix_plan):
        raise ValidationError("fix plan does not match executed plan")
    verification_source = verification_result["source"]
    if verification_source.get("exec_result_sha256") != json_sha256(exec_result):
        raise ValidationError("exec result does not match verification result")
    fix_plan_source = fix_plan["source"]
    expected = {
        "analyzer_root": str(analyzer_paths["analyzer_root"]),
        "manifest_path": str(analyzer_paths["manifest_path"]),
        "run_id": analyzer_paths["run_id"],
        "publications": analyzer_paths["publications"],
    }
    if any(fix_plan_source.get(key) != value for key, value in expected.items()):
        raise ValidationError("analyzer output does not match fix plan source")


def _build_report_input(
    verification_result_path: Path,
    analyzer_output_path: Path,
    output_dir: Path,
    *,
    baseline_run_dir: Path | None,
    baseline_monitor_policy: str,
) -> dict[str, Any]:
    verification_result = read_json(verification_result_path)
    validate_verification_result(verification_result)
    analyzer_paths = resolve_analyzer_paths(analyzer_output_path)
    _validate_analyzer_provenance(verification_result, analyzer_paths)
    old_run, old_task_by_key, monitor_path = _build_old_run(
        analyzer_paths,
        output_dir,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
        agent=str(verification_result["agent"]),
    )
    task_results = [
        _with_old_status(item, old_task_by_key)
        for item in verification_result.get("task_results", [])
        if isinstance(item, dict)
    ]
    unexpected_run_task_results = [
        item
        for item in verification_result.get("unexpected_run_task_results", [])
        if isinstance(item, dict)
    ]
    safe_verification_result = {
        **verification_result,
        "rerun": report_rerun_facts(verification_result.get("rerun")),
    }
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_report_input",
        "source": _report_source(
            verification_result_path,
            analyzer_paths,
            baseline_run_dir,
            monitor_path,
        )
        | {
            "verification_rerun_stdout_path": str(
                verification_result["rerun"].get("stdout_path") or ""
            ),
            "verification_rerun_stderr_path": str(
                verification_result["rerun"].get("stderr_path") or ""
            ),
        },
        "baseline_monitor_policy": baseline_monitor_policy,
        "verification_result": safe_verification_result,
        "old_run": old_run,
        "task_results": task_results,
        "unexpected_run_task_results": unexpected_run_task_results,
        "summary_input": _summary_input(
            str(verification_result.get("status") or "inconclusive"),
            old_run,
            verification_result,
            task_results,
            baseline_monitor_required=baseline_monitor_policy == "on",
            baseline_monitor_available=bool(old_run.get("monitor_available")),
        ),
    }
    return payload


def _apply_summary_caveats(
    summary: dict[str, Any], report_input: dict[str, Any]
) -> dict[str, Any]:
    caveats = report_input["summary_input"].get("caveats") or []
    if caveats:
        summary = {
            **summary,
            "caveats": list(dict.fromkeys([*summary.get("caveats", []), *caveats])),
        }
    if report_input["baseline_monitor_policy"] == "on" and not report_input[
        "old_run"
    ].get("monitor_available"):
        summary = {
            **summary,
            "status": "failed",
            "generation_errors": [
                *summary.get("generation_errors", []),
                {
                    "stage": "baseline_monitor",
                    "error": "baseline monitor data required by policy but unavailable",
                },
            ],
        }
    return summary


def _generate_report(
    report_input: dict[str, Any], output_dir: Path, invoker: AgentInvoker
) -> dict[str, Any]:
    write_json_atomic(output_dir / "report-input.json", report_input)

    verification_result = report_input["verification_result"]
    fix_plan_path = _fix_plan_path(verification_result)
    summary, raw_paths = generate_report_summary(
        invoker, report_input["summary_input"], output_dir
    )
    target_environment_path = fix_plan_path.parent / "target-environment.json"
    target_context_path = fix_plan_path.parent / "target-context.json"
    result_payload = {
        "summary": _apply_summary_caveats(summary, report_input),
        "schema_version": 1,
        "kind": "harbor_fixer_report",
        "generated_at": _utc_now(),
        "agent": verification_result["agent"],
        "status": verification_result["status"],
        "execution": verification_result["execution"],
        "reason_codes": verification_result["reason_codes"],
        "source": report_input["source"],
        "old_run": report_input["old_run"],
        "new_run": {
            "summary": verification_result["new_run_summary"],
            "rerun": report_rerun_facts(verification_result.get("rerun")),
            "monitor_output_path": verification_result["source"].get(
                "monitor_output_path", ""
            ),
            "verification_mode": verification_result.get(
                "verification_mode", "full_run"
            ),
            "sampling": verification_result.get("sampling", {}),
        },
        "plan_results": verification_result["plan_results"],
        "task_results": report_input["task_results"],
        "unexpected_run_task_results": report_input.get(
            "unexpected_run_task_results", []
        ),
        "artifacts": {
            "fix_plan_path": verification_result["source"].get("fix_plan_path", ""),
            "exec_result_path": verification_result["source"].get(
                "exec_result_path", ""
            ),
            "verification_result_path": report_input["source"][
                "verification_result_path"
            ],
            "report_input_path": str(output_dir / "report-input.json"),
            "target_environment_path": (
                str(target_environment_path)
                if target_environment_path.is_file()
                else ""
            ),
            "target_context_path": (
                str(target_context_path) if target_context_path.is_file() else ""
            ),
            "human_report_path": "",
            "verification_rerun_stdout_path": report_input["source"].get(
                "verification_rerun_stdout_path", ""
            ),
            "verification_rerun_stderr_path": report_input["source"].get(
                "verification_rerun_stderr_path", ""
            ),
            "raw_summary_output_paths": raw_paths,
        },
    }
    validate_fix_report(result_payload, verification_result=verification_result)
    write_json_atomic(output_dir / "fix-report-latest.json", result_payload)
    return result_payload


def generate_report_from_paths(
    verification_result_path: Path,
    analyzer_output_path: Path,
    output_dir: Path,
    invoker: AgentInvoker,
    *,
    baseline_run_dir: Path | None = None,
    baseline_monitor_policy: str = "auto",
) -> dict[str, Any]:
    verification_result_path = verification_result_path.expanduser().resolve()
    analyzer_output_path = analyzer_output_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if baseline_run_dir is not None:
        baseline_run_dir = baseline_run_dir.expanduser().resolve()
    _prepare_report_output(output_dir)
    if baseline_monitor_policy not in {"auto", "on", "off"}:
        raise ValidationError("baseline_monitor_policy must be one of: auto, off, on")
    report_input = _build_report_input(
        verification_result_path,
        analyzer_output_path,
        output_dir,
        baseline_run_dir=baseline_run_dir,
        baseline_monitor_policy=baseline_monitor_policy,
    )
    return _generate_report(report_input, output_dir, invoker)
