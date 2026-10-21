"""Deterministic T1 execution policy for simple commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifact_io import read_json
from ..command_analysis import CommandAnalysis, analyze_command
from ..validation import (
    ValidationError,
    require_dict,
    require_enum,
    require_list,
    require_string,
)
from .builtin import destructive_reason, read_only_reason

AGENT_ONLY_EXECUTABLES = {
    "bash",
    "command",
    "dash",
    "docker",
    "env",
    "eval",
    "exec",
    "find",
    "git",
    "ksh",
    "nohup",
    "sh",
    "su",
    "sudo",
    "timeout",
    "xargs",
    "zsh",
}


@dataclass(frozen=True)
class PrefixRule:
    rule_id: str
    pattern: tuple[str, ...]
    match: str
    decision: str


def _is_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and name.replace("_", "a").isalnum()


def normalize_argv(argv: list[str]) -> list[str]:
    """Unwrap narrow execution forms for deterministic deny matching."""

    original = list(argv)
    result = list(argv)
    while result:
        while result and _is_assignment(result[0]):
            result.pop(0)
        if not result:
            break
        wrapper = result[0]
        if wrapper not in {"env", "sudo"}:
            break
        result.pop(0)
        while result and result[0].startswith("-"):
            option = result.pop(0)
            if option == "--":
                break
            if (
                wrapper == "env"
                and option in {"-u", "--unset", "-C", "--chdir"}
                and result
            ) or (
                wrapper == "sudo"
                and option in {"-D", "--chdir", "-g", "--group", "-u", "--user"}
                and result
            ):
                result.pop(0)
            else:
                return original
    return result


def _rule_argv(analysis: CommandAnalysis) -> tuple[list[list[str]], list[str]] | None:
    if analysis.classification != "static_argv":
        return None
    argv = list(analysis.argv)
    normalized = normalize_argv(argv)
    if not normalized:
        return None
    candidates = [argv]
    if normalized != argv:
        candidates.append(normalized)
    static_argv = list(analysis.argv)
    allow_argv = (
        []
        if not static_argv
        or analysis.has_embedded_script
        or Path(argv[0]).name in AGENT_ONLY_EXECUTABLES
        else static_argv
    )
    return candidates, allow_argv


def _validate_rule(item: Any, name: str, decision: str) -> PrefixRule:
    payload = require_dict(item, name)
    pattern = tuple(
        require_string(value, f"{name}.pattern[{index}]")
        for index, value in enumerate(
            require_list(payload.get("pattern"), f"{name}.pattern")
        )
    )
    if not pattern:
        raise ValidationError(f"{name}.pattern must be non-empty")
    match = require_enum(
        payload.get("match", "exact"), f"{name}.match", {"exact", "prefix"}
    )
    if decision == "allow" and match == "prefix" and len(pattern) < 2:
        raise ValidationError(
            f"{name} prefix allow pattern must contain at least two tokens"
        )
    return PrefixRule(
        rule_id=require_string(payload.get("rule_id"), f"{name}.rule_id"),
        pattern=pattern,
        match=match,
        decision=decision,
    )


def load_user_rules(path: Path | None) -> tuple[list[PrefixRule], str]:
    if path is None:
        return [], ""
    payload = read_json(path)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "harbor_fixer_policy_rules"
    ):
        raise ValidationError(
            "policy rules must be harbor_fixer_policy_rules schema_version 1"
        )
    rules: list[PrefixRule] = []
    for decision in ("deny", "allow"):
        for index, item in enumerate(require_list(payload.get(decision, []), decision)):
            rules.append(_validate_rule(item, f"{decision}[{index}]", decision))
    return rules, str(path)


def _matches(rule: PrefixRule, argv: list[str]) -> bool:
    if rule.match == "exact":
        return tuple(argv) == rule.pattern
    return tuple(argv[: len(rule.pattern)]) == rule.pattern


def evaluate_t1(
    action: dict[str, Any],
    user_rules: list[PrefixRule],
    *,
    analysis: CommandAnalysis | None = None,
    executable_verified: bool = False,
) -> dict[str, Any] | None:
    """Return a deterministic T1 decision for a simple command, if available."""

    action_sha256 = analyze_command(action).action_sha256
    command_analysis = (
        analysis
        if analysis is not None and analysis.action_sha256 == action_sha256
        else analyze_command(action)
    )
    parsed = _rule_argv(command_analysis)
    if parsed is None:
        return None
    candidates, allow_argv = parsed
    if not candidates:
        return None
    for argv in candidates:
        deny_reason = destructive_reason(argv)
        if deny_reason is not None:
            reason_code, executable = deny_reason
            return {
                "tier": "T1",
                "decision": "deny",
                "risk_level": "high",
                "source": "builtin_rule",
                "rule_id": reason_code,
                "reason_code": reason_code,
                "reason": f"built-in policy prohibits destructive command: {executable}",
            }
    for rule in (rule for rule in user_rules if rule.decision == "deny"):
        if any(_matches(rule, argv) for argv in candidates):
            return {
                "tier": "T1",
                "decision": "deny",
                "risk_level": "high",
                "source": "user_rule",
                "rule_id": rule.rule_id,
                "reason_code": "user_deny_rule",
                "reason": f"command matches user deny rule {rule.rule_id}",
            }
    if not allow_argv or not executable_verified:
        return None
    user_allow = next(
        (
            rule
            for rule in user_rules
            if rule.decision == "allow" and _matches(rule, allow_argv)
        ),
        None,
    )
    if user_allow is not None:
        return {
            "tier": "T1",
            "decision": "allow",
            "risk_level": "low",
            "source": "user_rule",
            "rule_id": user_allow.rule_id,
            "reason_code": "t1_allow_rule",
            "reason": "command matches user allow rule",
        }
    builtin = read_only_reason(allow_argv)
    if builtin is None:
        return None
    return {
        "tier": "T1",
        "decision": "allow",
        "risk_level": "low",
        "source": "builtin_rule",
        "rule_id": builtin[0],
        "reason_code": "t1_allow_rule",
        "reason": builtin[1],
    }
