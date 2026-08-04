"""Write a human-readable benchmark summary from Monitor and Analyzer artifacts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from harbor_analyzer.io import write_json_atomic, write_text_atomic
from harbor_pi_runtime import PiProcessResult, run_pi_json_process

SUMMARY_SYSTEM_PROMPT = """You summarize one Harbor benchmark run for its user.
Use only the supplied JSON. Do not invent task names, counts, causes, or actions.
Return exactly one JSON object with this shape:
{
  "summary": "At most two concise sentences.",
  "analysis_summary": [
    {"group_id": "G1", "summary": "One concise plain-text explanation."}
  ],
  "recommended_actions": [
    {"group_ids": ["G1"], "action": "One concise evidence-based action."}
  ]
}
Include every supplied analysis group exactly once in analysis_summary. For an
analysis_failed group, explain why Analyzer failed; do not claim that this is
the benchmark task's root cause. Use at most three recommended actions. Do not
return Markdown."""

FINDING_LABELS = {
    "success": "No failure found",
    "env_fail": "Environment failure",
    "infra_fail": "Infrastructure failure",
    "model_fail": "Model failure",
    "unknown": "Unknown root cause",
}


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return payload


def _format_runtime(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _task_label(analysis: dict[str, Any]) -> str:
    task = analysis.get("task", {})
    name = str(task.get("task_name") or task.get("task_index") or "unknown-task")
    attempt_id = task.get("attempt_id")
    return f"{name} ({attempt_id})" if attempt_id else name


def _task_identity(task: dict[str, Any]) -> tuple[Any, Any]:
    return task.get("task_index"), task.get("task_name")


def _final_failed_task_ids(monitor: dict[str, Any]) -> set[tuple[Any, Any]] | None:
    handover = monitor.get("task_handover")
    if not isinstance(handover, list):
        return None
    return {
        _task_identity(task)
        for task in handover
        if isinstance(task, dict)
        and task.get("task_complete_status")
        in {"complete_failed", "complete_unknown", "not_complete"}
    }


def _analysis_coverage(
    eligible_task_ids: set[tuple[Any, Any]] | None,
    analyses: list[dict[str, Any]],
) -> dict[str, int] | None:
    if eligible_task_ids is None:
        return None
    analyzed_task_ids = {
        _task_identity(task)
        for analysis in analyses
        if analysis.get("analysis_status") in {"analysis_complete", "analysis_failed"}
        and isinstance((task := analysis.get("task")), dict)
    }
    return {
        "expected": len(eligible_task_ids),
        "analyzed": len(analyzed_task_ids & eligible_task_ids),
    }


def _analyzer_tasks(
    manifest_path: Path,
    *,
    expected_run_id: str | None,
    eligible_task_ids: set[tuple[Any, Any]] | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    if not manifest_path.is_file():
        return None, []

    manifest = _load_json(manifest_path)
    manifest_run_id = manifest.get("run_id")
    if expected_run_id and manifest_run_id != expected_run_id:
        return None, []
    tasks_by_identity: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for publication in manifest.get("publications", []):
        report_path = Path(publication["artifacts"]["benchmark_report_path"])
        report = _load_json(report_path)
        for analysis in report.get("tasks", []):
            task = analysis.get("task", {})
            if eligible_task_ids is not None and _task_identity(task) not in eligible_task_ids:
                continue
            identity = (
                task.get("task_index"),
                task.get("task_name"),
                task.get("attempt_id"),
            )
            tasks_by_identity[identity] = analysis
    return manifest_run_id, list(tasks_by_identity.values())


def _append_unique(values: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def _analysis_groups(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for analysis in analyses:
        status = analysis.get("analysis_status")
        if status == "analysis_failed":
            reason = analysis.get("root_cause_summary")
            key = (status, reason)
        elif status == "analysis_complete":
            key = (
                status,
                analysis.get("final_class"),
                analysis.get("root_cause_code"),
            )
        else:
            continue

        group = groups.setdefault(
            key,
            {
                "analysis_status": status,
                "final_class": analysis.get("final_class"),
                "root_cause_code": analysis.get("root_cause_code"),
                "failure_stages": [],
                "tasks": [],
                "root_cause_summaries": [],
            },
        )
        _append_unique(group["tasks"], _task_label(analysis))
        _append_unique(group["failure_stages"], analysis.get("failure_stage"))
        _append_unique(group["root_cause_summaries"], analysis.get("root_cause_summary"))

    result = []
    for index, group in enumerate(groups.values(), start=1):
        result.append({"group_id": f"G{index}", **group})
    return result


def _summary_input(
    monitor: dict[str, Any],
    manifest_path: Path,
    expected_run_id: str | None,
) -> dict[str, Any]:
    evidence = monitor.get("evidence", {})
    task_summary = monitor.get("task_summary", {})
    total = int(task_summary.get("total_evaluated", 0))
    success = int(task_summary.get("complete_success", 0))
    failure = sum(
        int(task_summary.get(status, 0))
        for status in ("complete_failed", "complete_unknown", "not_complete")
    )
    final_failed_task_ids = _final_failed_task_ids(monitor)
    run_id, analyses = _analyzer_tasks(
        manifest_path,
        expected_run_id=expected_run_id,
        eligible_task_ids=final_failed_task_ids,
    )
    findings = {
        "analysis_complete": 0,
        "env_fail": 0,
        "infra_fail": 0,
        "model_fail": 0,
        "unknown": 0,
        "analysis_failed": 0,
    }
    for analysis in analyses:
        status = analysis.get("analysis_status")
        if status == "analysis_failed":
            findings["analysis_failed"] += 1
        elif status == "analysis_complete":
            findings["analysis_complete"] += 1
            final_class = analysis.get("final_class")
            if final_class in {"env_fail", "infra_fail", "model_fail", "unknown"}:
                findings[final_class] += 1

    analysis_groups = _analysis_groups(analyses)
    return {
        "run": {
            "run_id": expected_run_id or run_id or os.environ.get("RUN_ID", "unknown"),
            "runtime": _format_runtime(float(evidence.get("elapsed_since_run_start", 0))),
            "total_tasks": total,
            "successful_tasks": success,
            "failed_tasks": failure,
            "success_rate": f"{success / total:.2%}" if total else "0.00%",
            "failure_rate": f"{failure / total:.2%}" if total else "0.00%",
        },
        "analyzer_findings": findings,
        "analysis_groups": analysis_groups,
        "analyzer_coverage": _analysis_coverage(final_failed_task_ids, analyses),
        "analyzer_result_status": (
            "not_required"
            if failure == 0
            else "available"
            if analysis_groups
            else "unavailable"
        ),
    }


def _summary_prompt(payload: dict[str, Any]) -> str:
    return (
        "Summarize this benchmark JSON according to the required output schema.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )


def _validate_model_output(
    payload: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = payload.get("summary")
    analysis_summary = payload.get("analysis_summary")
    recommended_actions = payload.get("recommended_actions")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary model output has no summary")
    if not isinstance(analysis_summary, list) or not isinstance(recommended_actions, list):
        raise TypeError("summary model output has invalid lists")

    expected_ids = {group["group_id"] for group in groups}
    observed_ids: list[str] = []
    for item in analysis_summary:
        if not isinstance(item, dict):
            raise TypeError("summary model output has invalid analysis_summary item")
        group_id = item.get("group_id")
        text = item.get("summary")
        if group_id not in expected_ids or not isinstance(text, str) or not text.strip():
            raise ValueError("summary model output has invalid analysis_summary fields")
        observed_ids.append(group_id)
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != expected_ids:
        raise ValueError("summary model output does not cover each analysis group once")

    for item in recommended_actions:
        if not isinstance(item, dict):
            raise TypeError("summary model output has invalid recommended action")
        group_ids = item.get("group_ids")
        action = item.get("action")
        if (
            not isinstance(group_ids, list)
            or not group_ids
            or any(group_id not in expected_ids for group_id in group_ids)
            or not isinstance(action, str)
            or not action.strip()
        ):
            raise ValueError("summary model output has invalid recommended action fields")
    if len(recommended_actions) > 3:
        raise ValueError("summary model output has too many recommended actions")
    return payload


def _run_summary_model(
    payload: dict[str, Any],
    summary_dir: Path,
) -> tuple[dict[str, Any] | None, PiProcessResult]:
    result = run_pi_json_process(
        prompt=_summary_prompt(payload),
        events_path=summary_dir / "events.jsonl",
        stderr_path=summary_dir / "stderr.txt",
        runtime_home=summary_dir / ".pi-home",
        runtime_workdir=summary_dir / ".pi-work",
        pi_bin="pi",
        provider=os.environ.get("HARBOR_ANALYZER_PI_PROVIDER", "harbor-analyzer"),
        model=os.environ.get("HARBOR_ANALYZER_MODEL", ""),
        base_url=os.environ.get("HARBOR_ANALYZER_BASE_URL", ""),
        api_key_env="HARBOR_ANALYZER_API_KEY",
        agent_name="harbor-benchmark-summarizer",
        display_name="Harbor Benchmark Summarizer",
        timeout_seconds=int(os.environ.get("HARBOR_ANALYZER_TIMEOUT", "900")),
        launch_mode="independent_pi_benchmark_summarizer",
        system_prompt=SUMMARY_SYSTEM_PROMPT,
        no_proxy_env="HARBOR_ANALYZER_NO_PROXY",
        prompt_in_stdin=True,
        no_tools=True,
        no_builtin_tools=True,
        disable_extensions=True,
        disable_skills=True,
        disable_prompt_templates=True,
        disable_context_files=True,
        auth_header=True,
    )
    if result.block_reason or result.output_json is None:
        return None, result
    try:
        return _validate_model_output(result.output_json, payload["analysis_groups"]), result
    except (TypeError, ValueError) as exc:
        result.block_reason = f"pi_summary_output_invalid:{exc}"
        return None, result


def _fallback_model_output(payload: dict[str, Any]) -> dict[str, Any]:
    analysis_summary = []
    for group in payload["analysis_groups"]:
        summaries = group["root_cause_summaries"]
        text = summaries[0] if summaries else "No analysis summary is available."
        analysis_summary.append({"group_id": group["group_id"], "summary": text})
    return {
        "summary": "The automated narrative summary is unavailable; deterministic results are shown below.",
        "analysis_summary": analysis_summary,
        "recommended_actions": [],
    }


def _inline_tasks(tasks: list[str]) -> str:
    return ", ".join(f"`{task.replace('`', '')}`" for task in tasks)


def _render_markdown(
    payload: dict[str, Any],
    model_output: dict[str, Any],
) -> str:
    run = payload["run"]
    findings = payload["analyzer_findings"]
    groups = {group["group_id"]: group for group in payload["analysis_groups"]}
    model_summaries = {
        item["group_id"]: " ".join(item["summary"].split())
        for item in model_output["analysis_summary"]
    }
    lines = [
        "# Benchmark Run Summary",
        "",
        f"Run ID: `{run['run_id']}`",
        "",
        "## Summary",
        "",
        " ".join(model_output["summary"].split()),
        "",
        "## Run Overview",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Runtime | {run['runtime']} |",
        f"| Success rate | {run['success_rate']} ({run['successful_tasks']}/{run['total_tasks']}) |",
        f"| Failure rate | {run['failure_rate']} ({run['failed_tasks']}/{run['total_tasks']}) |",
        "",
        "## Analyzer Findings",
        "",
        "| Finding | Tasks |",
        "| --- | ---: |",
        f"| Analysis complete | {findings['analysis_complete']} |",
        f"| Environment failure | {findings['env_fail']} |",
        f"| Infrastructure failure | {findings['infra_fail']} |",
        f"| Model failure | {findings['model_fail']} |",
        f"| Unknown root cause | {findings['unknown']} |",
        f"| Analysis failed | {findings['analysis_failed']} |",
        "",
        "## Analysis Summary",
        "",
    ]
    coverage = payload["analyzer_coverage"]
    if coverage and 0 < coverage["analyzed"] < coverage["expected"]:
        lines.extend(
            [
                (
                    "Analyzer results are incomplete: analysis is available for "
                    f"{coverage['analyzed']} of {coverage['expected']} final "
                    "failed/unknown/not-complete task(s)."
                ),
                "",
            ]
        )
    if groups:
        for group_id, group in groups.items():
            label = (
                "Analysis failed"
                if group["analysis_status"] == "analysis_failed"
                else FINDING_LABELS.get(group["final_class"], "Analysis")
            )
            text = model_summaries[group_id]
            if group["analysis_status"] == "analysis_failed":
                text += " The task's root cause remains undetermined."
            lines.append(f"- **{label} - {_inline_tasks(group['tasks'])}:** {text}")
    elif payload["analyzer_result_status"] == "unavailable":
        lines.append(
            "Analyzer results are unavailable for "
            f"{run['failed_tasks']} failed/unknown/not-complete task(s); "
            "inspect Analyzer handovers and stderr."
        )
    else:
        lines.append("No failed task required Analyzer work.")

    lines.extend(["", "## Recommended Actions", ""])
    actions = model_output["recommended_actions"]
    if actions:
        for index, item in enumerate(actions, start=1):
            tasks = []
            for group_id in item["group_ids"]:
                for task in groups[group_id]["tasks"]:
                    if task not in tasks:
                        tasks.append(task)
            lines.append(
                f"{index}. **{_inline_tasks(tasks)}:** {' '.join(item['action'].split())}"
            )
    else:
        lines.append("No additional action was generated.")
    return "\n".join(lines) + "\n"


def write_benchmark_summary(
    monitor_path: Path,
    manifest_path: Path,
    output_path: Path,
    expected_run_id: str | None = None,
) -> None:
    monitor = _load_json(monitor_path)
    if monitor.get("benchmark_status") == "running":
        raise ValueError("monitor still reports benchmark_status=running")

    payload = _summary_input(monitor, manifest_path, expected_run_id)
    summary_dir = output_path.parent / "benchmark-summary"
    summary_output_path = summary_dir / "summary-output.json"
    write_json_atomic(summary_dir / "summary-input.json", payload)
    summary_output_path.unlink(missing_ok=True)
    model_output, model_result = _run_summary_model(payload, summary_dir)
    if model_output is None:
        print(
            f"benchmark summary model unavailable: {model_result.block_reason}",
            file=sys.stderr,
        )
        model_output = _fallback_model_output(payload)
    else:
        write_json_atomic(summary_output_path, model_output)
    write_text_atomic(output_path, _render_markdown(payload, model_output))


def main() -> int:
    if len(sys.argv) not in {4, 5}:
        print(
            f"usage: {Path(sys.argv[0]).name} MONITOR_JSON ANALYZER_MANIFEST OUTPUT_MD [RUN_ID]",
            file=sys.stderr,
        )
        return 2
    try:
        write_benchmark_summary(
            *(Path(value) for value in sys.argv[1:4]),
            expected_run_id=sys.argv[4] if len(sys.argv) == 5 else None,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"benchmark summary not written: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
