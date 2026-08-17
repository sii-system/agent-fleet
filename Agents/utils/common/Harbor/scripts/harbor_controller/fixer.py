"""Minimal user-controlled Harbor Fixer workflow."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harbor_fixer.agent_invocation import PiAgentInvoker, PiInvocationConfig
from harbor_fixer.artifact_io import read_json, write_json_atomic
from harbor_fixer.executor import MAX_SUMMARY_LIMIT, run_fix_exec_from_plan
from harbor_fixer.plan_generation import (
    MAX_TASK_SUMMARIES_CHARS,
    MAX_TASK_SUMMARY_CHARS,
    run_plan_generation,
)
from harbor_fixer.policy import run_policy_preflight

ACTIVE_STATUSES = {
    "planning",
    "policy_review",
    "awaiting_approval",
    "executing",
    "cancelling",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fixer_dir(run_dir: Path) -> Path:
    return run_dir / "fixer"


def _state_path(run_dir: Path) -> Path:
    return _fixer_dir(run_dir) / "fixer-state.json"


def _control_request_path(run_dir: Path) -> Path:
    return _fixer_dir(run_dir) / "fixer-control-request.json"


def _approval_request_path(run_dir: Path) -> Path:
    return _fixer_dir(run_dir) / "fixer-approval-request.json"


def _decision_path(run_dir: Path) -> Path:
    return _fixer_dir(run_dir) / "fixer-user-decision.json"


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return read_json(path)


@contextmanager
def _state_lock(run_dir: Path) -> Iterator[None]:
    path = _fixer_dir(run_dir) / ".fixer-control.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_env_int(name: str, default: int, *, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be positive{suffix}")
    return value


def _runtime_config(
    *,
    analyzer_output: Path,
    workspace_root: Path,
    policy_rules_path: Path | None,
    policy_write_roots: list[Path],
) -> dict[str, Any]:
    if not workspace_root.is_dir():
        raise ValueError(f"workspace root is not a directory: {workspace_root}")
    if not analyzer_output.is_dir():
        raise ValueError(f"analyzer output is not a directory: {analyzer_output}")
    config = {
        "analyzer_output": str(analyzer_output.resolve()),
        "workspace_root": str(workspace_root.resolve()),
        "policy_rules_path": str(policy_rules_path.resolve()) if policy_rules_path else "",
        "policy_write_roots": [
            str(path.resolve()) for path in dict.fromkeys(policy_write_roots)
        ],
        "pi_bin": os.environ.get("HARBOR_FIXER_PI_BIN", "pi"),
        "pi_provider": os.environ.get("HARBOR_FIXER_PI_PROVIDER", "harbor-fixer"),
        "pi_model": os.environ.get("HARBOR_FIXER_MODEL") or os.environ.get("MODEL", ""),
        "pi_base_url": os.environ.get("HARBOR_FIXER_BASE_URL")
        or os.environ.get("BASE_URL", ""),
        "pi_api_key_env": "HARBOR_FIXER_API_KEY",
        "agent_timeout": _positive_env_int("HARBOR_FIXER_AGENT_TIMEOUT", 900),
        "execution_timeout": _positive_env_int("HARBOR_FIXER_EXECUTION_TIMEOUT", 300),
        "summary_limit": _positive_env_int(
            "HARBOR_FIXER_SUMMARY_LIMIT",
            4000,
            maximum=MAX_SUMMARY_LIMIT,
        ),
        "max_concurrency": _positive_env_int("HARBOR_FIXER_MAX_CONCURRENCY", 4),
        "max_task_summary_chars": _positive_env_int(
            "HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS",
            MAX_TASK_SUMMARY_CHARS,
        ),
        "max_task_summaries_chars": _positive_env_int(
            "HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS",
            MAX_TASK_SUMMARIES_CHARS,
        ),
    }
    return config


def _pi_config(config: dict[str, Any]) -> PiInvocationConfig:
    api_key_env = str(config["pi_api_key_env"])
    if not os.environ.get(api_key_env) and os.environ.get("API_KEY"):
        os.environ[api_key_env] = os.environ["API_KEY"]
    return PiInvocationConfig(
        pi_bin=str(config["pi_bin"]),
        provider=str(config["pi_provider"]),
        model=str(config["pi_model"]),
        base_url=str(config["pi_base_url"]),
        api_key_env=api_key_env,
        timeout_seconds=int(config["agent_timeout"]),
    )


def _notification(run_dir: Path) -> dict[str, Any]:
    return read_json(run_dir / "monitor" / "user-notify-latest.json")


def _validate_start_inputs(run_dir: Path, analyzer_output: Path) -> str:
    notification = _notification(run_dir)
    benchmark_status = str(notification.get("benchmark_status") or "")
    if benchmark_status not in {"completed", "stopped"}:
        raise ValueError(
            "Fixer can start only after the benchmark is completed or explicitly stopped"
        )
    run_id = str(notification.get("run_id") or run_dir.name)
    manifest = read_json(analyzer_output / "analyzer-artifacts-latest.json")
    if manifest.get("run_id") != run_id:
        raise ValueError("Analyzer output run_id does not match the current benchmark")
    return run_id


def _write_control_request(
    run_dir: Path,
    *,
    run_id: str,
    workflow_id: str,
    action: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_control_request",
        "request_id": f"request-{uuid.uuid4()}",
        "run_id": run_id,
        "fixer_workflow_id": workflow_id,
        "action": action,
        "created_at": _utc_now(),
    }
    write_json_atomic(_control_request_path(run_dir), payload)
    return payload


def _state_for_workflow(run_dir: Path, workflow_id: str) -> dict[str, Any]:
    state = read_json(_state_path(run_dir))
    if state.get("fixer_workflow_id") != workflow_id:
        raise ValueError("Fixer workflow no longer matches the current state")
    return state


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    write_json_atomic(_state_path(run_dir), state)


def _transition(
    run_dir: Path,
    workflow_id: str,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        state.update(status=status, **fields)
        _write_state(run_dir, state)
        return state


def _cancel_requested(run_dir: Path, workflow_id: str) -> bool:
    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        return state.get("status") == "cancelling"


def _finish_cancelled(run_dir: Path, workflow_id: str) -> dict[str, Any]:
    return _transition(
        run_dir,
        workflow_id,
        "cancelled",
        finished_at=_utc_now(),
        outcome="cancelled_before_execution",
        available_actions=["start"],
    )


def _advance_or_cancel(
    run_dir: Path,
    workflow_id: str,
    *,
    expected_status: str,
    next_status: str,
) -> dict[str, Any]:
    """Advance one pre-execution stage without overwriting a concurrent cancel."""

    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        if state.get("status") == "cancelling":
            state.update(
                status="cancelled",
                finished_at=_utc_now(),
                outcome="cancelled_before_execution",
                available_actions=["start"],
            )
        elif state.get("status") == expected_status:
            state.update(status=next_status, available_actions=["cancel"])
        else:
            raise ValueError(
                f"Fixer cannot advance from status {state.get('status')}"
            )
        _write_state(run_dir, state)
        return state


def _run_planning(config: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    pi_config = _pi_config(config)
    return run_plan_generation(
        Path(config["analyzer_output"]),
        output_dir,
        PiAgentInvoker(output_dir, replace(pi_config, thinking_level="off")),
        PiAgentInvoker(output_dir, pi_config),
        max_concurrency=int(config["max_concurrency"]),
        max_task_summary_chars=int(config["max_task_summary_chars"]),
        max_task_summaries_chars=int(config["max_task_summaries_chars"]),
        workspace_root=Path(config["workspace_root"]),
    )


def _policy_denials(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "plan_id": decision.get("plan_id"),
            "action_id": decision.get("action_id"),
            "reason_code": decision.get("reason_code"),
            "reason": decision.get("reason"),
        }
        for decision in policy.get("decisions", [])
        if isinstance(decision, dict) and decision.get("decision") == "deny"
    ]


def _approval_plans(fix_plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "plan_id": plan["plan_id"],
            "fix_scope": plan["fix_scope"],
            "tasks": plan["task_list"],
            "fix_reason": plan["fix_reason"],
            "actions": plan["actions"],
        }
        for plan in fix_plan.get("plans", [])
    ]


def _finish_empty_plan(run_dir: Path, workflow_id: str) -> dict[str, Any]:
    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        if state.get("status") == "cancelling":
            state.update(status="cancelled", outcome="cancelled_before_execution")
        elif state.get("status") == "planning":
            state.update(status="completed", outcome="no_actions")
        else:
            raise ValueError(f"Fixer cannot finish from status {state.get('status')}")
        state.update(finished_at=_utc_now(), available_actions=["start"])
        _write_state(run_dir, state)
        return state


def _finish_policy_review(
    run_dir: Path,
    workflow_id: str,
    *,
    fix_plan: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        if state.get("status") == "cancelling":
            state.update(
                status="cancelled",
                outcome="cancelled_before_execution",
                finished_at=_utc_now(),
                available_actions=["start"],
            )
            _write_state(run_dir, state)
            return state
        if state.get("status") != "policy_review":
            raise ValueError(
                f"Fixer cannot finish policy review from status {state.get('status')}"
            )
        if policy["status"] == "denied":
            state.update(
                status="blocked",
                outcome="policy_denied",
                finished_at=_utc_now(),
                policy_status="denied",
                policy_denials=_policy_denials(policy),
                available_actions=["start"],
            )
            _write_state(run_dir, state)
            return state

        fix_plan_sha256 = _json_sha256(fix_plan)
        approval_request_id = "sha256-" + hashlib.sha256(
            f"{state['run_id']}\0{workflow_id}\0{fix_plan_sha256}".encode()
        ).hexdigest()
        approval = {
            "schema_version": 1,
            "kind": "harbor_fixer_approval_request",
            "run_id": state["run_id"],
            "fixer_workflow_id": workflow_id,
            "approval_request_id": approval_request_id,
            "fix_plan_sha256": fix_plan_sha256,
            "policy_status": "allowed",
            "created_at": _utc_now(),
            "plans": _approval_plans(fix_plan),
        }
        write_json_atomic(_approval_request_path(run_dir), approval)
        state.update(
            status="awaiting_approval",
            approval_request_id=approval_request_id,
            fix_plan_sha256=fix_plan_sha256,
            policy_status="allowed",
            plan_count=len(fix_plan["plans"]),
            action_count=sum(len(plan["actions"]) for plan in fix_plan["plans"]),
            available_actions=["approve", "cancel"],
        )
        _write_state(run_dir, state)
        return state


def start_fixer(
    run_dir: Path,
    *,
    workspace_root: Path,
    analyzer_output: Path | None = None,
    policy_rules_path: Path | None = None,
    policy_write_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Generate and policy-check one plan, stopping before mutation."""

    run_dir = run_dir.resolve()
    analyzer_output = (analyzer_output or run_dir / "analyzer").resolve()
    output_dir = _fixer_dir(run_dir)
    run_id = _validate_start_inputs(run_dir, analyzer_output)
    config = _runtime_config(
        analyzer_output=analyzer_output,
        workspace_root=workspace_root.resolve(),
        policy_rules_path=policy_rules_path,
        policy_write_roots=policy_write_roots or [],
    )
    workflow_id = f"fixer-{uuid.uuid4()}"
    with _state_lock(run_dir):
        current = _load_optional_json(_state_path(run_dir))
        if current and current.get("status") in ACTIVE_STATUSES:
            raise ValueError(
                "another Fixer workflow is active: "
                f"{current.get('fixer_workflow_id')} ({current.get('status')})"
            )
        request = _write_control_request(
            run_dir,
            run_id=run_id,
            workflow_id=workflow_id,
            action="start",
        )
        for path in (_approval_request_path(run_dir), _decision_path(run_dir)):
            path.unlink(missing_ok=True)
        state = {
            "schema_version": 1,
            "kind": "harbor_fixer_workflow_state",
            "run_id": run_id,
            "fixer_workflow_id": workflow_id,
            "status": "planning",
            "outcome": "",
            "started_at": _utc_now(),
            "updated_at": _utc_now(),
            "finished_at": "",
            "control_request_id": request["request_id"],
            "approval_request_id": "",
            "fix_plan_sha256": "",
            "config": config,
            "paths": {
                "fix_plan": str(output_dir / "fix-plan-latest.json"),
                "policy_decision": str(output_dir / "execution-policy-decision.json"),
                "exec_result": str(output_dir / "exec-result-latest.json"),
                "verification_result": "",
                "fix_report": "",
                "benchmark_summary": str(run_dir / "analyzer" / "benchmark-summary.md"),
            },
            "available_actions": ["cancel"],
            "error": None,
            "verification_status": "not_available",
            "report_status": "not_available",
        }
        _write_state(run_dir, state)

    try:
        fix_plan = _run_planning(config, output_dir)
        if not fix_plan.get("plans"):
            return _finish_empty_plan(run_dir, workflow_id)

        state = _advance_or_cancel(
            run_dir,
            workflow_id,
            expected_status="planning",
            next_status="policy_review",
        )
        if state["status"] == "cancelled":
            return state
        policy = run_policy_preflight(
            fix_plan,
            Path(config["workspace_root"]),
            output_dir,
            PiAgentInvoker(output_dir, _pi_config(config)),
            user_rules_path=(
                Path(config["policy_rules_path"])
                if config["policy_rules_path"]
                else None
            ),
            writable_roots=[Path(value) for value in config["policy_write_roots"]],
        )
        return _finish_policy_review(
            run_dir,
            workflow_id,
            fix_plan=fix_plan,
            policy=policy,
        )
    except Exception as exc:
        if _cancel_requested(run_dir, workflow_id):
            return _finish_cancelled(run_dir, workflow_id)
        _transition(
            run_dir,
            workflow_id,
            "failed",
            outcome="planning_or_policy_failed",
            finished_at=_utc_now(),
            available_actions=["start"],
            error={"stage": "planning_or_policy", "message": str(exc)},
        )
        raise ValueError(f"Fixer planning failed: {exc}") from exc


def _write_user_decision(
    run_dir: Path,
    *,
    state: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "kind": "harbor_fixer_user_decision",
        "run_id": state["run_id"],
        "fixer_workflow_id": state["fixer_workflow_id"],
        "approval_request_id": state.get("approval_request_id", ""),
        "fix_plan_sha256": state.get("fix_plan_sha256", ""),
        "decision_id": f"decision-{uuid.uuid4()}",
        "decision": decision,
        "created_at": _utc_now(),
    }
    write_json_atomic(_decision_path(run_dir), payload)
    return payload


def _execution_counts(result: dict[str, Any]) -> dict[str, int]:
    counts = {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    for plan in result.get("plans", []):
        if not isinstance(plan, dict):
            continue
        for action in plan.get("actions", []):
            if not isinstance(action, dict):
                continue
            counts["total"] += 1
            status = str(action.get("status") or "")
            key = {"success": "succeeded", "failed": "failed", "skipped": "skipped"}.get(
                status
            )
            if key:
                counts[key] += 1
    return counts


def approve_fixer(run_dir: Path, approval_request_id: str) -> dict[str, Any]:
    """Execute the exact currently approved plan."""

    run_dir = run_dir.resolve()
    with _state_lock(run_dir):
        state = read_json(_state_path(run_dir))
        if state.get("status") != "awaiting_approval":
            raise ValueError("Fixer is not awaiting approval")
        if state.get("approval_request_id") != approval_request_id:
            raise ValueError("approval request does not match the current Fixer plan")
        approval = read_json(_approval_request_path(run_dir))
        if approval.get("approval_request_id") != approval_request_id:
            raise ValueError("approval artifact does not match the requested approval")
        for key in ("run_id", "fixer_workflow_id", "fix_plan_sha256"):
            if approval.get(key) != state.get(key):
                raise ValueError(f"approval artifact {key} does not match Fixer state")
        fix_plan_path = Path(state["paths"]["fix_plan"])
        fix_plan = read_json(fix_plan_path)
        observed_sha256 = _json_sha256(fix_plan)
        if observed_sha256 != approval.get("fix_plan_sha256"):
            state.update(
                status="blocked",
                outcome="fix_plan_changed_after_review",
                finished_at=_utc_now(),
                available_actions=["start"],
                error={"stage": "approval", "message": "Fix Plan changed after review"},
            )
            _write_state(run_dir, state)
            raise ValueError("Fix Plan changed after the approval request was created")
        request = _write_control_request(
            run_dir,
            run_id=str(state["run_id"]),
            workflow_id=str(state["fixer_workflow_id"]),
            action="approve",
        )
        decision = _write_user_decision(run_dir, state=state, decision="approve")
        state.update(
            status="executing",
            control_request_id=request["request_id"],
            decision_id=decision["decision_id"],
            available_actions=[],
            error=None,
        )
        _write_state(run_dir, state)

    config = state["config"]
    try:
        result = run_fix_exec_from_plan(
            fix_plan_path,
            _fixer_dir(run_dir),
            Path(config["workspace_root"]),
            policy_invoker=PiAgentInvoker(_fixer_dir(run_dir), _pi_config(config)),
            policy_rules_path=(
                Path(config["policy_rules_path"])
                if config["policy_rules_path"]
                else None
            ),
            policy_write_roots=[Path(value) for value in config["policy_write_roots"]],
            execution_timeout_seconds=int(config["execution_timeout"]),
            summary_limit=int(config["summary_limit"]),
        )
        if result.get("policy_status") == "denied":
            return _transition(
                run_dir,
                state["fixer_workflow_id"],
                "blocked",
                outcome="policy_denied_at_execution",
                finished_at=_utc_now(),
                policy_status="denied",
                execution_counts=_execution_counts(result),
                available_actions=["start"],
            )
        workflow_status = "completed" if result.get("status") == "success" else "failed"
        return _transition(
            run_dir,
            state["fixer_workflow_id"],
            workflow_status,
            outcome=str(result.get("status") or "failed"),
            finished_at=_utc_now(),
            policy_status=str(result.get("policy_status") or ""),
            execution_counts=_execution_counts(result),
            available_actions=["start"],
        )
    except Exception as exc:
        _transition(
            run_dir,
            state["fixer_workflow_id"],
            "failed",
            outcome="execution_failed",
            finished_at=_utc_now(),
            available_actions=["start"],
            error={"stage": "executing", "message": str(exc)},
        )
        raise ValueError(f"Fixer execution failed: {exc}") from exc


def cancel_fixer(run_dir: Path, workflow_id: str) -> dict[str, Any]:
    """Reject a pending plan or request cancellation before execution."""

    run_dir = run_dir.resolve()
    with _state_lock(run_dir):
        state = _state_for_workflow(run_dir, workflow_id)
        status = str(state.get("status") or "")
        if status == "executing":
            raise ValueError(
                "Fixer execution cannot be safely cancelled mid-action; wait for the current execution result"
            )
        if status not in {"planning", "policy_review", "awaiting_approval"}:
            raise ValueError(f"Fixer cannot be cancelled from status {status}")
        request = _write_control_request(
            run_dir,
            run_id=str(state["run_id"]),
            workflow_id=workflow_id,
            action="cancel",
        )
        decision = _write_user_decision(run_dir, state=state, decision="cancel")
        if status == "awaiting_approval":
            state.update(
                status="cancelled",
                outcome="user_rejected_plan",
                finished_at=_utc_now(),
                control_request_id=request["request_id"],
                decision_id=decision["decision_id"],
                available_actions=["start"],
            )
        else:
            state.update(
                status="cancelling",
                outcome="cancel_requested",
                control_request_id=request["request_id"],
                decision_id=decision["decision_id"],
                available_actions=[],
            )
        _write_state(run_dir, state)
        return state


def fixer_status(run_dir: Path) -> dict[str, Any]:
    """Return the user-facing Fixer state for controller status."""

    run_dir = run_dir.resolve()
    state = _load_optional_json(_state_path(run_dir))
    paths = {
        "state": str(_state_path(run_dir)),
        "approval_request": str(_approval_request_path(run_dir)),
        "decision": str(_decision_path(run_dir)),
        "benchmark_summary": str(run_dir / "analyzer" / "benchmark-summary.md"),
        "fix_report": str(_fixer_dir(run_dir) / "fix-report-latest.md"),
    }
    if state is None:
        return {
            "status": "not_started",
            "available_actions": ["start"],
            "verification_status": "not_available",
            "report_status": "not_available",
            "paths": paths,
        }

    visible = {
        key: state.get(key)
        for key in (
            "run_id",
            "fixer_workflow_id",
            "status",
            "outcome",
            "started_at",
            "updated_at",
            "finished_at",
            "approval_request_id",
            "policy_status",
            "plan_count",
            "action_count",
            "execution_counts",
            "policy_denials",
            "error",
            "verification_status",
            "report_status",
            "available_actions",
        )
        if state.get(key) not in (None, "")
    }
    visible["paths"] = {**paths, **state.get("paths", {})}
    if state.get("status") == "awaiting_approval":
        visible["approval"] = read_json(_approval_request_path(run_dir))
    return visible
