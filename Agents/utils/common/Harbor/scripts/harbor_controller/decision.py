"""Validate and consume explicit Harbor user decisions."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def build_decision_request_id(run_id: str, incident_key: str) -> str:
    encoded = f"{run_id}\0{incident_key}".encode()
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


def _read_decision(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _consumed_decisions(state: dict[str, Any]) -> list[str]:
    raw = state.get("consumed_user_decision_ids")
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw if isinstance(value, str) and value]


def _record_consumed(state: dict[str, Any], decision_id: str) -> None:
    consumed = _consumed_decisions(state)
    if decision_id not in consumed:
        consumed.append(decision_id)
    state["consumed_user_decision_ids"] = consumed[-100:]


def _wait_action(
    requested_action: dict[str, Any],
    *,
    decision_id: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "type": "wait",
        "retry_count": requested_action.get("retry_count", 0),
        "reason": "user_approved_wait",
        "allowed_decisions": [],
        "decision_required": False,
        "controller_status": "observing",
        "external_control_performed": False,
        "decision_id": decision_id,
        "decision_request_id": request_id,
        "submitted_decision": "wait",
        "decision_status": "executed",
    }


def resolve_user_decision(
    requested_action: dict[str, Any],
    *,
    decision_path: Path,
    request_id: str | None,
    run_id: str,
    state: dict[str, Any],
    now: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a user-selected action only for the current notification."""

    if requested_action.get("type") != "notify" or not request_id:
        return requested_action, None
    allowed = requested_action.get("allowed_decisions")
    if not isinstance(allowed, list) or not allowed:
        return requested_action, None

    current_time = time.time() if now is None else now
    deferred = state.get("deferred_user_wait")
    if isinstance(deferred, dict) and deferred.get("decision_request_id") == request_id:
        try:
            wait_until = float(deferred.get("wait_until"))
        except (TypeError, ValueError):
            wait_until = 0
        if current_time < wait_until:
            decision_id = str(deferred.get("decision_id") or "")
            return (
                _wait_action(
                    requested_action,
                    decision_id=decision_id,
                    request_id=request_id,
                ),
                {
                    "decision_id": decision_id,
                    "decision_request_id": request_id,
                    "decision": "wait",
                    "status": "executed",
                    "wait_until": wait_until,
                },
            )
        state["deferred_user_wait"] = None

    payload = _read_decision(decision_path)
    if payload is None:
        return requested_action, None
    decision_id = str(payload.get("decision_id") or "")
    decision = str(payload.get("decision") or "")
    if not decision_id or decision_id in _consumed_decisions(state):
        return requested_action, None

    record: dict[str, Any] = {
        "decision_id": decision_id,
        "decision_request_id": str(payload.get("decision_request_id") or ""),
        "decision": decision,
        "status": "rejected",
    }
    if payload.get("run_id") != run_id:
        record["reason"] = "run_id_mismatch"
        return requested_action, record
    if payload.get("decision_request_id") != request_id:
        record["reason"] = "decision_request_id_mismatch"
        return requested_action, record
    if decision not in allowed:
        record["reason"] = "decision_not_allowed"
        return requested_action, record

    if decision == "wait":
        wait_seconds = payload.get("wait_seconds", 300)
        if not isinstance(wait_seconds, int) or wait_seconds <= 0:
            record["status"] = "rejected"
            record["reason"] = "wait_seconds_invalid"
            return requested_action, record
        _record_consumed(state, decision_id)
        wait_until = current_time + min(wait_seconds, 86400)
        state["deferred_user_wait"] = {
            "decision_id": decision_id,
            "decision_request_id": request_id,
            "wait_until": wait_until,
        }
        record["status"] = "executed"
        record["wait_until"] = wait_until
        return (
            _wait_action(
                requested_action,
                decision_id=decision_id,
                request_id=request_id,
            ),
            record,
        )

    _record_consumed(state, decision_id)
    record["status"] = "accepted"
    action = {
        "type": decision,
        "retry_count": requested_action.get("retry_count", 0),
        "reason": "user_approved",
        "allowed_decisions": [],
        "decision_required": False,
        "controller_status": "executing_user_decision",
        "external_control_performed": False,
        "decision_id": decision_id,
        "decision_request_id": request_id,
        "submitted_decision": decision,
        "decision_status": "accepted",
    }
    return action, record
