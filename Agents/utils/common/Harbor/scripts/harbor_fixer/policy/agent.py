"""Isolated T2 and T3 Policy Agent evaluation."""

from __future__ import annotations

import re
from typing import Any

from harbor_pi_runtime import PiInvocationError, sleep_before_retry

from ..agent_invocation import AgentInvoker
from ..prompts import build_validation_retry_prompt
from ..validation import (
    ValidationError,
    parse_strict_json_object,
    require_enum,
    require_string,
)
from .prompts import T2_POLICY_AGENT_PROMPT, T3_POLICY_AGENT_PROMPT

POLICY_DECISIONS = {"allow", "deny"}
RISK_LEVELS = {"low", "medium", "high"}


def _validate_decision(
    payload: dict[str, Any],
    *,
    tier: str,
    plan_id: str,
    action_id: str,
) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "tier",
        "plan_id",
        "action_id",
        "decision",
        "risk_level",
        "reason_code",
        "reason",
    }
    if set(payload) != expected_fields:
        raise ValidationError("policy agent decision fields do not match contract")
    if payload.get("schema_version") != 1:
        raise ValidationError("policy agent decision schema_version must be 1")
    if payload.get("kind") != "harbor_fixer_policy_agent_decision":
        raise ValidationError("policy agent decision kind is invalid")
    if payload.get("tier") != tier:
        raise ValidationError("policy agent decision tier does not match input")
    if payload.get("plan_id") != plan_id or payload.get("action_id") != action_id:
        raise ValidationError("policy agent decision identity does not match input")
    require_enum(payload.get("decision"), "decision", POLICY_DECISIONS)
    require_enum(payload.get("risk_level"), "risk_level", RISK_LEVELS)
    reason_code = require_string(payload.get("reason_code"), "reason_code")
    if re.fullmatch(r"[a-z][a-z0-9_]*", reason_code) is None:
        raise ValidationError("reason_code must be lower snake_case")
    require_string(payload.get("reason"), "reason")


def evaluate_agent(
    invoker: AgentInvoker | None,
    policy_input: dict[str, Any],
) -> dict[str, Any]:
    """Return an Agent verdict or a fail-closed fallback decision."""

    tier = policy_input["tier"]
    plan_id = policy_input["plan_id"]
    action_id = policy_input["action"]["action_id"]
    base_prompt = T2_POLICY_AGENT_PROMPT if tier == "T2" else T3_POLICY_AGENT_PROMPT
    if invoker is None:
        return {
            "tier": tier,
            "decision": "deny",
            "risk_level": "high",
            "source": "policy_agent_fallback",
            "rule_id": "",
            "reason_code": "policy_agent_unavailable",
            "reason": "policy agent is required for actions not resolved by T1",
        }
    prompt = base_prompt
    previous_output = ""
    for attempt in (1, 2):
        try:
            raw = invoker.invoke(
                prompt,
                policy_input,
                attempt=attempt,
                label=f"policy-{tier.lower()}-{plan_id}-{action_id}",
            )
            previous_output = raw
            decision = parse_strict_json_object(raw)
            _validate_decision(
                decision,
                tier=tier,
                plan_id=plan_id,
                action_id=action_id,
            )
            return {
                "tier": tier,
                "decision": decision["decision"],
                "risk_level": decision["risk_level"],
                "source": "policy_agent",
                "rule_id": "",
                "reason_code": decision["reason_code"],
                "reason": decision["reason"],
            }
        except Exception as exc:  # noqa: BLE001 - policy errors must fail closed
            last_error = f"{exc.__class__.__name__}: {exc}"
            if isinstance(exc, PiInvocationError) and not exc.retryable:
                break
            prompt = build_validation_retry_prompt(
                base_prompt=base_prompt,
                previous_output=previous_output,
                validation_error=last_error,
            )
            if attempt < 2:
                sleep_before_retry(attempt)
    return {
        "tier": tier,
        "decision": "deny",
        "risk_level": "high",
        "source": "policy_agent_fallback",
        "rule_id": "",
        "reason_code": "policy_agent_failed_closed",
        "reason": last_error,
    }
