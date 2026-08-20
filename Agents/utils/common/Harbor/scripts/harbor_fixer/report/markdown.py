"""Deterministic Markdown rendering for Harbor Fixer reports."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from ..artifact_io import read_json, write_json_atomic, write_text_atomic
from ..planning_context.workspace_evidence import redact_sensitive_text

SECRET_NAME_PATTERN = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|password|client[_-]?secret|"
    r"secret[_-]?access[_-]?key|secret|private[_-]?key)"
)
SECRET_VALUE_PATTERN = r"[^\r\n]+"
SECRET_ASSIGNMENT_RE = re.compile(
    rf"((?<![a-z0-9_-])(?:--|['\"])?{SECRET_NAME_PATTERN}['\"]?\s*[=:]\s*)"
    rf"((?!['\"]?<REDACTED>){SECRET_VALUE_PATTERN})",
    re.IGNORECASE,
)
SECRET_ARGUMENT_ASSIGNMENT_RE = re.compile(
    rf"^(\s*(?:--)?{SECRET_NAME_PATTERN}\s*[=:]\s*).*$",
    re.IGNORECASE,
)
SECRET_ARGUMENT_OPTION_RE = re.compile(rf"^--{SECRET_NAME_PATTERN}$", re.IGNORECASE)
SECRET_OPTION_RE = re.compile(
    rf"((?<![a-z0-9_-])--{SECRET_NAME_PATTERN}\s+)"
    rf"((?!['\"]?<REDACTED>){SECRET_VALUE_PATTERN})",
    re.IGNORECASE,
)


def _redact_human_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", text)
    text = SECRET_OPTION_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", text)
    return redact_sensitive_text(text)


def _bounded_human_text(value: Any, limit: int = 4000) -> str:
    text = _redact_human_text(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n<TRUNCATED>"


def _markdown_cell(value: Any) -> str:
    text = _bounded_human_text(value, 2000)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _markdown_code(value: Any, language: str = "") -> str:
    text = _bounded_human_text(value, 12000)
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)),
        default=0,
    )
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _markdown_quote(value: Any) -> str:
    text = _bounded_human_text(value)
    if not text:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |" for row in rows
    )
    return "\n".join(lines)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _read_optional_artifact(
    path_value: Any, *, relative_to: Path | None = None
) -> tuple[dict[str, Any], str]:
    value = str(path_value or "")
    if not value:
        return {}, "artifact path is empty"
    path = Path(value)
    if relative_to is not None and not path.is_absolute() and not path.is_file():
        path = relative_to / path
    if not path.is_file():
        return {}, f"artifact is missing or unreadable: {path}"
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"artifact could not be read: {path}: {exc.__class__.__name__}: {exc}"
    if not isinstance(payload.get("plans"), list):
        return payload, f"artifact field is unavailable or invalid: {path}: plans"
    return payload, ""


def _plan_by_id(fix_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("plan_id") or ""): item
        for item in _list(fix_plan.get("plans"))
        if isinstance(item, dict) and item.get("plan_id")
    }


def _exec_by_plan_id(exec_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("plan_id") or ""): item
        for item in _list(exec_result.get("plans"))
        if isinstance(item, dict) and item.get("plan_id")
    }


def _planned_action_text(action: dict[str, Any]) -> tuple[str, str]:
    if action.get("action_type") == "command":
        argv = [str(action.get("executable") or "")]
        argv.extend(str(value) for value in _list(action.get("arguments")))
        argv = [
            (
                "<REDACTED>"
                if index and SECRET_ARGUMENT_OPTION_RE.fullmatch(argv[index - 1])
                else SECRET_ARGUMENT_ASSIGNMENT_RE.sub(
                    lambda match: f"{match.group(1)}<REDACTED>", value
                )
            )
            for index, value in enumerate(argv)
        ]
        return shlex.join(argv), "bash"
    payload = {
        "path": action.get("path", ""),
        "edit": action.get("edit", {}),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), "json"


def _run_summary_rows(report: dict[str, Any]) -> list[list[Any]]:
    old_summary = report.get("old_run", {}).get("monitor_summary", {})
    new_summary = report.get("new_run", {}).get("summary", {})
    rows: list[list[Any]] = []
    for label, key in (
        ("Total", "total"),
        ("Complete success", "complete_success"),
        ("Complete failed", "complete_failed"),
        ("Complete unknown", "complete_unknown"),
        ("Not complete", "not_complete"),
        ("Success rate", "success_rate"),
    ):
        rows.append(
            [
                label,
                old_summary.get(key, "Unavailable"),
                new_summary.get(key, "Unavailable"),
            ]
        )
    return rows


def _task_outcome_label(item: dict[str, Any]) -> str:
    if item.get("unexpected"):
        return "Unexpected rerun result"
    if item.get("sampled") is False:
        return "Not sampled"
    if item.get("sampled") is not True:
        return "Sampling unavailable"
    status = str(item.get("verification_status") or "unknown")
    labels = {
        "fixed": "Sampled — fixed",
        "not_fixed": "Sampled — not fixed",
        "exec_failed": "Sampled — execution failed",
        "not_complete": "Sampled — not complete",
        "unknown": "Sampled — inconclusive",
    }
    return labels.get(status, f"Sampled — {status}")


def _verification_scope_rows(report: dict[str, Any]) -> list[list[Any]]:
    new_run = report.get("new_run", {})
    summary = new_run.get("summary", {})
    sampling = new_run.get("sampling", {})
    verification_mode = str(new_run.get("verification_mode") or "")
    task_results = [
        item for item in report.get("task_results", []) if isinstance(item, dict)
    ]
    sampled = [item for item in task_results if item.get("sampled")]
    execution = report.get("execution", {})
    return [
        ["Agent", report.get("agent", "Unavailable") or "Unavailable"],
        ["Verification status", report.get("status", "Unavailable")],
        ["Execution status", execution.get("status", "Unavailable")],
        ["Execution policy status", execution.get("policy_status", "Unavailable")],
        [
            "Recorded reason codes",
            ", ".join(str(value) for value in report.get("reason_codes", [])) or "None",
        ],
        [
            "Status scope",
            "Sampled tasks only"
            if verification_mode == "smoke_test"
            else ("Verification run" if verification_mode else "Unavailable"),
        ],
        ["Verification mode", verification_mode or "Unavailable"],
        [
            "Planned tasks",
            summary.get(
                "plan_task_count", sampling.get("plan_task_count", "Unavailable")
            ),
        ],
        [
            "Sampled tasks",
            summary.get(
                "sampled_task_count", sampling.get("sampled_task_count", "Unavailable")
            ),
        ],
        [
            "Unsampled tasks",
            summary.get(
                "unsampled_task_count",
                sampling.get("unsampled_task_count", "Unavailable"),
            ),
        ],
        [
            "Sampled — fixed",
            sum(item.get("verification_status") == "fixed" for item in sampled),
        ],
        [
            "Sampled — not fixed",
            sum(item.get("verification_status") == "not_fixed" for item in sampled),
        ],
        [
            "Sampled — other/inconclusive",
            sum(
                item.get("verification_status") not in {"fixed", "not_fixed"}
                for item in sampled
            ),
        ],
        ["Generated at", report.get("generated_at", "Unavailable") or "Unavailable"],
    ]


def _problem_rows(report: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in report.get("old_run", {}).get("tasks", []):
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        analyzer = (
            item.get("analyzer") if isinstance(item.get("analyzer"), dict) else {}
        )
        rows.append(
            [
                task.get("task_index", ""),
                task.get("task_name", ""),
                analyzer.get("final_class", ""),
                analyzer.get("failure_stage", ""),
                analyzer.get("scope", ""),
                analyzer.get("root_cause_code", ""),
                analyzer.get("root_cause_summary", ""),
                analyzer.get("confidence", "Unavailable")
                if analyzer.get("confidence") is not None
                else "Unavailable",
            ]
        )
    return rows


def _task_result_rows(report: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    task_results = list(report.get("task_results", []))
    task_results.extend(
        {
            "task": {
                "task_index": record["task_index"],
                "task_name": record["task_name"],
            },
            "unexpected": True,
            "new_run": record,
            "verification_status": "unexpected",
        }
        for record in report.get("unexpected_run_task_results", [])
    )
    for item in task_results:
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        new_run = item.get("new_run") if isinstance(item.get("new_run"), dict) else {}
        evidence = (
            new_run.get("evidence") if isinstance(new_run.get("evidence"), dict) else {}
        )
        evidence_fields: list[str] = []
        for label, key in (
            ("reward", "reward_raw"),
            ("rc", "rc"),
            ("exception", "exception_type"),
            ("early_stop", "early_stop_reason"),
        ):
            if evidence.get(key) not in {None, ""}:
                evidence_fields.append(f"{label}={evidence[key]}")
        signals = new_run.get("task_result_signals")
        if isinstance(signals, list) and signals:
            evidence_fields.append(
                "signals=" + ",".join(str(value) for value in signals)
            )
        if item.get("exec_failure_reason"):
            evidence_fields.append(f"exec_failure_reason={item['exec_failure_reason']}")
        rows.append(
            [
                task.get("task_index", ""),
                task.get("task_name", ""),
                item.get("plan_id", ""),
                _task_outcome_label(item),
                item.get("old_run_monitor_status") or "Unavailable",
                item.get("exec_status", ""),
                new_run.get("task_complete_status", "Not evaluated"),
                "; ".join(evidence_fields) or "No evidence fields recorded",
                item.get("verification_status", ""),
            ]
        )
    order = {
        "Sampled — fixed": 0,
        "Sampled — not fixed": 1,
        "Sampled — execution failed": 2,
        "Sampled — not complete": 3,
        "Sampled — inconclusive": 4,
        "Unexpected rerun result": 5,
        "Not sampled": 6,
        "Sampling unavailable": 7,
    }
    return sorted(rows, key=lambda row: (order.get(str(row[3]), 4), str(row[0])))


def _failure_rows(
    report: dict[str, Any],
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for error in _list(fix_plan.get("generation_errors")):
        rows.append(["plan", "-", json.dumps(error, ensure_ascii=False)])
    for plan in _list(exec_result.get("plans")):
        if not isinstance(plan, dict):
            continue
        plan_id = str(plan.get("plan_id") or "-")
        for action in _list(plan.get("actions")):
            if not isinstance(action, dict) or action.get("status") == "success":
                continue
            reason = (
                action.get("stderr_summary")
                or action.get("stdout_summary")
                or action.get("skip_reason")
                or f"action exited with {action.get('exit_code')}"
            )
            rows.append(["exec", f"{plan_id}/{action.get('action_id', '-')}", reason])
    rerun = report.get("new_run", {}).get("rerun", {})
    if rerun.get("skipped_reason"):
        rows.append(
            ["verification", "rerun", f"rerun skipped: {rerun['skipped_reason']}"]
        )
    elif rerun.get("exit_code") not in {None, 0}:
        rows.append(
            [
                "verification",
                "rerun",
                f"rerun exited with {rerun.get('exit_code')}",
            ]
        )
    if rerun.get("monitor_timed_out"):
        rows.append(["verification", "monitor", "monitor wait timed out"])
    sampling = report.get("new_run", {}).get("sampling", {})
    for error in sampling.get("selection_errors", []):
        rows.append(["verification", "sampling", json.dumps(error, ensure_ascii=False)])
    for error in sampling.get("mapping_errors", []):
        rows.append(["verification", "mapping", json.dumps(error, ensure_ascii=False)])
    for error in report.get("summary", {}).get("generation_errors", []):
        rows.append(["report", "summary", json.dumps(error, ensure_ascii=False)])
    for reason in report.get("reason_codes", []):
        rows.append(["verification", "reason_code", str(reason)])
    return rows


def _unavailable_rows(
    report: dict[str, Any], artifact_errors: list[str]
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    old_run = report.get("old_run", {})
    if not old_run.get("monitor_available"):
        rows.append(
            [
                "Baseline task results",
                "No baseline Monitor task results were available; comparison metrics are omitted.",
            ]
        )
    rerun = report.get("new_run", {}).get("rerun", {})
    if not rerun.get("monitor_available"):
        rows.append(
            [
                "Verification Monitor snapshot",
                "Monitor was disabled or no matching snapshot was available.",
            ]
        )
    for item in report.get("old_run", {}).get("tasks", []):
        if not isinstance(item, dict) or item.get("old_run_status"):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        identity = str(
            task.get("task_index") or task.get("task_name") or "unknown task"
        )
        rows.append(
            [
                f"Original status for task {identity}",
                "No explicit status was present in Analyzer artifacts.",
            ]
        )
    rows.extend(["Artifact", error] for error in artifact_errors if error)
    return rows


def render_human_report(
    report: dict[str, Any],
    fix_plan: dict[str, Any],
    exec_result: dict[str, Any],
    *,
    artifact_errors: list[str] | None = None,
) -> str:
    """Render observed facts first and attribute all Analyzer/plan interpretations."""

    artifact_errors = artifact_errors or []
    old_run = report.get("old_run", {})
    summary = report.get("summary", {})
    new_run = report.get("new_run", {})
    rerun = new_run.get("rerun", {})
    plans = _plan_by_id(fix_plan)
    exec_plans = _exec_by_plan_id(exec_result)
    lines = [
        f"# Harbor Fixer Report: {_markdown_cell(old_run.get('run_id') or 'unknown run')}",
        "",
        "## Human summary",
        "",
        _markdown_quote(summary.get("text"))
        or "No model-generated summary was available.",
    ]
    if summary.get("highlights"):
        lines.extend(
            [
                "",
                "### Highlights",
                "",
                _markdown_table(
                    ["#", "Result"],
                    [
                        [index, value]
                        for index, value in enumerate(summary["highlights"], 1)
                    ],
                ),
            ]
        )
    if summary.get("caveats"):
        lines.extend(
            [
                "",
                "### Caveats",
                "",
                _markdown_table(
                    ["#", "Caveat"],
                    [
                        [index, value]
                        for index, value in enumerate(summary["caveats"], 1)
                    ],
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Observed Fixer results",
            "",
            "### Verification scope",
            "",
            _markdown_table(
                ["Field", "Recorded value"], _verification_scope_rows(report)
            ),
            "",
            _markdown_quote(
                "Verifier labels are shown with their recorded scope. In smoke-test mode they describe sampled tasks only."
            ),
            "",
            "### Task outcomes",
            "",
        ]
    )
    task_rows = _task_result_rows(report)
    lines.append(
        _markdown_table(
            [
                "Task",
                "Name",
                "Plans",
                "Sampling and result group",
                "Observed baseline status",
                "Execution",
                "Observed rerun status",
                "Recorded task evidence",
                "Verifier task status",
            ],
            task_rows,
        )
        if task_rows
        else "No planned task results were recorded."
    )

    lines.extend(["", "## Trial execution", ""])
    exec_rows: list[list[Any]] = []
    for plan in _list(exec_result.get("plans")):
        if not isinstance(plan, dict):
            continue
        for action in _list(plan.get("actions")):
            if not isinstance(action, dict):
                continue
            exec_rows.append(
                [
                    plan.get("plan_id", ""),
                    action.get("action_id", ""),
                    action.get("action_type", ""),
                    action.get("status", ""),
                    action.get("exit_code", ""),
                    action.get("duration_ms", ""),
                ]
            )
    lines.append(
        _markdown_table(
            ["Plan", "Action", "Type", "Status", "Exit code", "Duration ms"],
            exec_rows,
        )
        if exec_rows
        else "No command execution results were available."
    )
    for plan in _list(exec_result.get("plans")):
        if not isinstance(plan, dict):
            continue
        for action in _list(plan.get("actions")):
            if not isinstance(action, dict) or action.get("status") == "success":
                continue
            evidence = (
                action.get("stderr_summary")
                or action.get("stdout_summary")
                or action.get("skip_reason")
            )
            if evidence:
                lines.extend(
                    [
                        "",
                        f"**{_markdown_cell(plan.get('plan_id', '-'))}/{_markdown_cell(action.get('action_id', '-'))} failure evidence**",
                        "",
                        _markdown_quote(evidence),
                    ]
                )

    lines.extend(
        [
            "",
            "### Rerun",
            "",
            _markdown_table(
                [
                    "Exit code",
                    "Skipped reason",
                    "Monitor available",
                    "Monitor timed out",
                    "Duration ms",
                ],
                [
                    [
                        rerun.get("exit_code", ""),
                        rerun.get("skipped_reason", ""),
                        rerun.get("monitor_available", "Unavailable"),
                        rerun.get("monitor_timed_out", "Unavailable"),
                        rerun.get("duration_ms", "Unavailable"),
                    ]
                ],
            ),
        ]
    )
    lines.extend(["", "### Recorded run metrics", ""])
    if old_run.get("monitor_available"):
        lines.append(
            _markdown_table(
                ["Metric", "Baseline run", "Verification sample"],
                _run_summary_rows(report),
            )
        )
    else:
        lines.extend(
            [
                _markdown_quote(
                    "Baseline run metrics are unavailable; no zero-value baseline is assumed."
                ),
                "",
                _markdown_table(
                    ["Metric", "Verification sample"],
                    [[row[0], row[2]] for row in _run_summary_rows(report)],
                ),
            ]
        )
    lines.extend(["", "## Failures and interruptions", ""])
    failure_rows = _failure_rows(report, fix_plan, exec_result)
    lines.append(
        _markdown_table(["Stage", "Item", "Cause"], failure_rows)
        if failure_rows
        else "No plan-generation, execution, rerun, mapping, monitor, or report-generation error was recorded."
    )

    lines.extend(["", "## Unavailable or missing information", ""])
    unavailable_rows = _unavailable_rows(report, artifact_errors)
    lines.append(
        _markdown_table(["Information", "Reason"], unavailable_rows)
        if unavailable_rows
        else "No unavailable or missing report input was recorded."
    )

    lines.extend(
        [
            "",
            "## Attributed analysis",
            "",
            "### Analyzer findings",
            "",
            _markdown_quote(
                "The following classifications and root-cause descriptions are copied from Analyzer artifacts. Reporter did not re-evaluate them."
            ),
            "",
        ]
    )
    problem_rows = _problem_rows(report)
    lines.append(
        _markdown_table(
            [
                "Task",
                "Name",
                "Class",
                "Stage",
                "Scope",
                "Root-cause code",
                "Root-cause summary",
                "Confidence",
            ],
            problem_rows,
        )
        if problem_rows
        else "No Analyzer env/infra task findings were recorded."
    )

    lines.extend(
        [
            "",
            "### Fix plan generated by Fixer",
            "",
            _markdown_quote(
                "The following scopes, reasoning, actions, purposes, and expected effects are copied from the generated Fix Plan. Reporter did not re-evaluate them."
            ),
            "",
        ]
    )
    if not plans:
        lines.append(_markdown_quote("No readable fix plan was available."))
    for plan_id, plan in plans.items():
        fix_reason = (
            plan.get("fix_reason") if isinstance(plan.get("fix_reason"), dict) else {}
        )
        scope_comparison = (
            plan.get("analyzer_scope_comparison")
            if isinstance(plan.get("analyzer_scope_comparison"), dict)
            else {}
        )
        target_tasks = ", ".join(
            str(item.get("task_index") or item.get("task_name") or "")
            for item in _list(plan.get("task_list"))
            if isinstance(item, dict)
        )
        lines.extend(
            [
                f"#### {_markdown_cell(plan_id)}",
                "",
                _markdown_table(
                    ["Field", "Recorded value"],
                    [
                        ["Fix scope", plan.get("fix_scope", "")],
                        ["Target tasks", target_tasks],
                        [
                            "Analyzer-scope relation",
                            scope_comparison.get("relation", ""),
                        ],
                        [
                            "Execution status",
                            exec_plans.get(plan_id, {}).get("status", "Not recorded"),
                        ],
                    ],
                ),
                "",
                "**Plan summary**",
                "",
                _markdown_quote(fix_reason.get("summary"))
                or "No plan summary was recorded.",
                "",
                "**Plan reasoning**",
                "",
                _markdown_quote(fix_reason.get("reasoning"))
                or _markdown_quote(scope_comparison.get("reason"))
                or "No plan reasoning was recorded.",
            ]
        )
        for action in _list(plan.get("actions")):
            if not isinstance(action, dict):
                continue
            action_text, language = _planned_action_text(action)
            lines.extend(
                [
                    "",
                    f"##### {_markdown_cell(action.get('action_id', 'action'))}",
                    "",
                    _markdown_table(
                        ["Type", "Working directory", "Purpose", "Expected effect"],
                        [
                            [
                                action.get("action_type", ""),
                                action.get("cwd", ""),
                                action.get("purpose", ""),
                                action.get("expected_effect", ""),
                            ]
                        ],
                    ),
                    "",
                    _markdown_code(action_text, language),
                ]
            )
    unplanned_rows = [
        [
            item.get("task_index") or item.get("task_name") or "-",
            item.get("reason", "No reason recorded"),
        ]
        for item in _list(fix_plan.get("unplanned_tasks"))
        if isinstance(item, dict)
    ]
    if unplanned_rows:
        lines.extend(
            [
                "",
                "#### Unplanned tasks",
                "",
                _markdown_table(["Task", "Plan-recorded reason"], unplanned_rows),
            ]
        )

    lines.extend(["", "## Artifacts", ""])
    artifact_rows = [
        [name, value]
        for name, value in report.get("artifacts", {}).items()
        if value and name not in {"human_report_path", "raw_summary_output_paths"}
    ]
    lines.append(_markdown_table(["Artifact", "Path"], artifact_rows))
    return "\n".join(lines).rstrip() + "\n"


def write_report_markdown(
    report: dict[str, Any], output_dir: Path
) -> dict[str, Any]:
    """Render referenced artifacts and attach the Markdown path to the report."""

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    verification_result_path = Path(
        str(artifacts.get("verification_result_path") or "")
    )
    artifact_base = verification_result_path.parent
    fix_plan, fix_plan_error = _read_optional_artifact(
        artifacts.get("fix_plan_path"), relative_to=artifact_base
    )
    exec_result, exec_result_error = _read_optional_artifact(
        artifacts.get("exec_result_path"), relative_to=artifact_base
    )
    human_report_path = output_dir / "fix-report-latest.md"
    report["artifacts"] = {**artifacts, "human_report_path": str(human_report_path)}
    write_text_atomic(
        human_report_path,
        render_human_report(
            report,
            fix_plan,
            exec_result,
            artifact_errors=[fix_plan_error, exec_result_error],
        ),
    )
    write_json_atomic(output_dir / "fix-report-latest.json", report)
    return report
