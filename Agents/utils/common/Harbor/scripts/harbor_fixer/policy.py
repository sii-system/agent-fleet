"""Deterministic and agent-backed execution policy for Harbor Fixer."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_invocation import AgentInvoker
from .artifact_io import read_json, write_json_atomic
from .builtin_policy import builtin_destructive_reason, builtin_read_only_reason
from .prompts import (
    T2_POLICY_AGENT_PROMPT,
    T3_POLICY_AGENT_PROMPT,
    build_validation_retry_prompt,
)
from .validation import (
    ValidationError,
    parse_strict_json_object,
    require_dict,
    require_enum,
    require_list,
    require_string,
)

POLICY_VERSION = "fixer-policy-v1"
POLICY_DECISIONS = {"allow", "deny"}
RISK_LEVELS = {"low", "medium", "high"}
CONTROL_TOKENS = {";", "&&", "||", "|", "&"}
REDIRECTION_TOKENS = {
    ">",
    ">>",
    "<",
    "<<",
    "<<<",
    "<>",
    ">&",
    "&>",
    "&>>",
    "<&",
}
OUTPUT_REDIRECTION_TOKENS = {">", ">>", "<>", ">&", "&>", "&>>"}
SHELL_NAMES = {"bash", "dash", "ksh", "sh", "zsh"}
WRITE_EACH_OPERAND = {
    "chmod",
    "chown",
    "chgrp",
    "mkdir",
    "tee",
    "touch",
    "truncate",
}
WRITE_LAST_OPERAND = {"cp", "install", "ln", "mv", "sed"}


@dataclass(frozen=True)
class PrefixRule:
    rule_id: str
    pattern: tuple[str, ...]
    match: str
    decision: str


def _lex(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in CONTROL_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_tokens(segment: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(segment):
        token = segment[index]
        if token in REDIRECTION_TOKENS:
            index += 2
            continue
        if (
            token.isdigit()
            and index + 1 < len(segment)
            and segment[index + 1] in REDIRECTION_TOKENS
        ):
            index += 3
            continue
        result.append(token)
        index += 1
    while result:
        while result and "=" in result[0] and not result[0].startswith("="):
            name, _value = result[0].split("=", 1)
            if not name.replace("_", "a").isalnum():
                break
            result.pop(0)
        if not result:
            break
        wrapper = Path(result[0]).name
        if wrapper not in {"command", "env", "nohup", "sudo"}:
            break
        result.pop(0)
        while result and result[0].startswith("-"):
            option = result.pop(0)
            if (
                wrapper == "sudo"
                and option
                in {
                    "-C",
                    "-g",
                    "-h",
                    "-p",
                    "-r",
                    "-T",
                    "-t",
                    "-u",
                }
                and result
            ):
                result.pop(0)
    return result


def _nested_shell_commands(command_tokens: list[str]) -> list[str]:
    if not command_tokens or Path(command_tokens[0]).name not in SHELL_NAMES:
        return []
    for index, token in enumerate(command_tokens[1:], start=1):
        if token == "-c" and index + 1 < len(command_tokens):
            return [command_tokens[index + 1]]
        if (
            token.startswith("-")
            and "c" in token[1:]
            and index + 1 < len(command_tokens)
        ):
            return [command_tokens[index + 1]]
    return []


def _embedded_shell_commands(command: str) -> list[str]:
    substitutions = re.findall(r"\$\(([^()]*)\)", command)
    substitutions.extend(re.findall(r"`([^`]*)`", command))
    return substitutions


def _rule_segments(command: str, *, depth: int = 0) -> list[list[str]]:
    if depth > 4:
        return []
    tokens = _lex(command)
    segments = [_command_tokens(segment) for segment in _segments(tokens)]
    segments = [segment for segment in segments if segment]
    nested_segments: list[list[str]] = []
    for segment in segments:
        for nested in _nested_shell_commands(segment):
            nested_segments.extend(_rule_segments(nested, depth=depth + 1))
    return [*segments, *nested_segments]


def _builtin_deny_reason(command: str) -> tuple[str, str] | None:
    try:
        tokens = _lex(command)
    except ValueError:
        return None
    reason = builtin_destructive_reason(tokens)
    if reason is not None:
        return reason
    for segment in _segments(tokens):
        command_tokens = _command_tokens(segment)
        if not command_tokens:
            continue
        reason = builtin_destructive_reason(command_tokens)
        if reason is not None:
            return reason
        executable = Path(command_tokens[0]).name
        if executable == "xargs" and command_tokens[1:]:
            nested_reason = _builtin_deny_reason(" ".join(command_tokens[1:]))
            if nested_reason is not None:
                return nested_reason
        for nested in _nested_shell_commands(command_tokens):
            nested_reason = _builtin_deny_reason(nested)
            if nested_reason is not None:
                return nested_reason
    for nested in _embedded_shell_commands(command):
        nested_reason = _builtin_deny_reason(nested)
        if nested_reason is not None:
            return nested_reason
    return None


def _validate_rule(item: Any, name: str, decision: str) -> PrefixRule:
    payload = require_dict(item, name)
    rule_id = require_string(payload.get("rule_id"), f"{name}.rule_id")
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
    return PrefixRule(rule_id=rule_id, pattern=pattern, match=match, decision=decision)


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


def _rule_matches(rule: PrefixRule, command_tokens: list[str]) -> bool:
    if rule.match == "exact":
        return tuple(command_tokens) == rule.pattern
    return tuple(command_tokens[: len(rule.pattern)]) == rule.pattern


def evaluate_t1(command: str, user_rules: list[PrefixRule]) -> dict[str, Any] | None:
    deny_reason = _builtin_deny_reason(command)
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
    try:
        tokens = _lex(command)
    except ValueError:
        return None
    segments = [_command_tokens(segment) for segment in _segments(tokens)]
    segments = [segment for segment in segments if segment]
    try:
        rule_segments = _rule_segments(command)
    except ValueError:
        return None
    for rule in [rule for rule in user_rules if rule.decision == "deny"]:
        if any(_rule_matches(rule, segment) for segment in rule_segments):
            return {
                "tier": "T1",
                "decision": "deny",
                "risk_level": "high",
                "source": "user_rule",
                "rule_id": rule.rule_id,
                "reason_code": "user_deny_rule",
                "reason": f"command matches user deny rule {rule.rule_id}",
            }
    if any(token in REDIRECTION_TOKENS for token in tokens):
        return None
    if any(marker in command for marker in ("$(", "`", "<(", ">(")):
        return None
    matched: list[tuple[str, str, str]] = []
    for segment in segments:
        user_allow = next(
            (
                rule
                for rule in user_rules
                if rule.decision == "allow" and _rule_matches(rule, segment)
            ),
            None,
        )
        if user_allow is not None:
            matched.append(
                ("user_rule", user_allow.rule_id, "command matches user allow rule")
            )
            continue
        builtin = builtin_read_only_reason(segment)
        if builtin is None:
            return None
        matched.append(("builtin_rule", builtin[0], builtin[1]))
    if not matched:
        return None
    sources = {item[0] for item in matched}
    return {
        "tier": "T1",
        "decision": "allow",
        "risk_level": "low",
        "source": sources.pop() if len(sources) == 1 else "combined_rules",
        "rule_id": ",".join(item[1] for item in matched),
        "reason_code": "t1_allow_rule",
        "reason": "; ".join(item[2] for item in matched),
    }


def _operand_tokens(segment: list[str]) -> list[str]:
    command_tokens = _command_tokens(segment)
    if not command_tokens:
        return []
    return [token for token in command_tokens[1:] if not token.startswith("-")]


def _write_targets(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        target = tokens[index + 1]
        if token in OUTPUT_REDIRECTION_TOKENS and not (
            token in {">&", "<&"} and (target.isdigit() or target == "-")
        ):
            targets.append(target)
    for segment in _segments(tokens):
        command_tokens = _command_tokens(segment)
        if not command_tokens:
            continue
        executable = Path(command_tokens[0]).name
        operands = _operand_tokens(segment)
        if executable in WRITE_EACH_OPERAND:
            targets.extend(operands)
        elif executable in WRITE_LAST_OPERAND and operands:
            targets.append(operands[-1])
    return list(dict.fromkeys(targets))


def _is_dynamic_path(value: str) -> bool:
    return value.startswith("~") or any(
        marker in value for marker in ("$", "*", "?", "[", "]", "{", "}")
    )


def analyze_paths(
    command: str,
    cwd: Path,
    writable_roots: list[Path],
) -> dict[str, Any]:
    try:
        tokens = _lex(command)
    except ValueError as exc:
        return {
            "classification": "unknown_or_outside",
            "write_targets": [],
            "dynamic": True,
            "reason": f"shell_parse_error: {exc}",
        }
    targets: list[dict[str, Any]] = []
    dynamic = False
    for raw in _write_targets(tokens):
        if _is_dynamic_path(raw):
            dynamic = True
            targets.append({"raw": raw, "resolved": "", "inside_writable_roots": False})
            continue
        path = Path(raw)
        resolved = (path if path.is_absolute() else cwd / path).resolve()
        inside = any(resolved.is_relative_to(root) for root in writable_roots)
        targets.append(
            {
                "raw": raw,
                "resolved": str(resolved),
                "inside_writable_roots": inside,
            }
        )
    inside_only = (
        bool(targets)
        and not dynamic
        and all(target["inside_writable_roots"] for target in targets)
    )
    return {
        "classification": "inside_writable_roots"
        if inside_only
        else "unknown_or_outside",
        "write_targets": targets,
        "dynamic": dynamic,
        "reason": (
            "all detected write targets resolve inside writable roots"
            if inside_only
            else "no bounded inside-root write set could be established"
        ),
    }


def _validate_agent_decision(
    payload: dict[str, Any],
    *,
    tier: str,
    plan_id: str,
    command_id: str,
) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "tier",
        "plan_id",
        "command_id",
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
    if payload.get("plan_id") != plan_id or payload.get("command_id") != command_id:
        raise ValidationError("policy agent decision identity does not match input")
    require_enum(payload.get("decision"), "decision", POLICY_DECISIONS)
    require_enum(payload.get("risk_level"), "risk_level", RISK_LEVELS)
    reason_code = require_string(payload.get("reason_code"), "reason_code")
    if re.fullmatch(r"[a-z][a-z0-9_]*", reason_code) is None:
        raise ValidationError("reason_code must be lower snake_case")
    require_string(payload.get("reason"), "reason")


def _agent_decision(
    invoker: AgentInvoker | None,
    policy_input: dict[str, Any],
    *,
    max_attempts: int = 2,
) -> dict[str, Any]:
    tier = policy_input["tier"]
    plan_id = policy_input["plan_id"]
    command_id = policy_input["command"]["command_id"]
    base_prompt = T2_POLICY_AGENT_PROMPT if tier == "T2" else T3_POLICY_AGENT_PROMPT
    if invoker is None:
        return {
            "tier": tier,
            "decision": "deny",
            "risk_level": "high",
            "source": "policy_agent_fallback",
            "rule_id": "",
            "reason_code": "policy_agent_unavailable",
            "reason": "policy agent is required for commands not resolved by T1",
        }
    prompt = base_prompt
    previous_output = ""
    last_error = "policy agent returned no valid decision"
    for attempt in range(1, max_attempts + 1):
        try:
            raw = invoker.invoke(
                prompt,
                policy_input,
                attempt=attempt,
                label=f"policy-{tier.lower()}-{plan_id}-{command_id}",
            )
            previous_output = raw
            decision = parse_strict_json_object(raw)
            _validate_agent_decision(
                decision,
                tier=tier,
                plan_id=plan_id,
                command_id=command_id,
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
            prompt = build_validation_retry_prompt(
                base_prompt=base_prompt,
                previous_output=previous_output,
                validation_error=last_error,
            )
    return {
        "tier": tier,
        "decision": "deny",
        "risk_level": "high",
        "source": "policy_agent_fallback",
        "rule_id": "",
        "reason_code": "policy_agent_failed_closed",
        "reason": last_error,
    }


def _configuration_denial(
    fix_plan: dict[str, Any],
    error: Exception,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for plan in fix_plan["plans"]:
        for command in plan["commands"]:
            decisions.append(
                {
                    "plan_id": plan["plan_id"],
                    "command_id": command["command_id"],
                    "tier": "T1",
                    "decision": "deny",
                    "risk_level": "high",
                    "source": "policy_configuration",
                    "rule_id": "",
                    "reason_code": "policy_configuration_error",
                    "reason": f"{error.__class__.__name__}: {error}",
                    "path_analysis": {},
                }
            )
    return decisions


def run_policy_preflight(
    fix_plan: dict[str, Any],
    workspace_root: Path,
    output_dir: Path,
    invoker: AgentInvoker | None,
    *,
    user_rules_path: Path | None = None,
    writable_roots: list[Path] | None = None,
) -> dict[str, Any]:
    roots = [workspace_root, *(writable_roots or [])]
    resolved_roots = list(dict.fromkeys(root.resolve() for root in roots))
    try:
        user_rules, rules_source = load_user_rules(user_rules_path)
    except (OSError, ValidationError, ValueError) as exc:
        decisions = _configuration_denial(fix_plan, exc)
        rules_source = str(user_rules_path or "")
        serialized_user_rules: list[dict[str, Any]] = []
    else:
        serialized_user_rules = [
            {
                "rule_id": rule.rule_id,
                "pattern": list(rule.pattern),
                "match": rule.match,
                "decision": rule.decision,
            }
            for rule in user_rules
        ]
        decisions = []
        for plan in fix_plan["plans"]:
            for command in plan["commands"]:
                plan_id = plan["plan_id"]
                command_id = command["command_id"]
                cwd_value = Path(command["cwd"])
                cwd = (
                    cwd_value if cwd_value.is_absolute() else workspace_root / cwd_value
                ).resolve()
                path_analysis = analyze_paths(
                    command["command"],
                    cwd,
                    resolved_roots,
                )
                decision = evaluate_t1(command["command"], user_rules)
                if decision is None:
                    tier = (
                        "T2"
                        if path_analysis["classification"] == "inside_writable_roots"
                        else "T3"
                    )
                    policy_input = {
                        "schema_version": 1,
                        "kind": "harbor_fixer_policy_agent_input",
                        "policy_version": POLICY_VERSION,
                        "tier": tier,
                        "plan_id": plan_id,
                        "command": command,
                        "plan_context": {
                            "fix_scope": plan["fix_scope"],
                            "analyzer_scope_comparison": plan[
                                "analyzer_scope_comparison"
                            ],
                            "task_list": plan["task_list"],
                            "fix_reason": plan["fix_reason"],
                        },
                        "workspace_root": str(workspace_root),
                        "writable_roots": [str(root) for root in resolved_roots],
                        "resolved_cwd": str(cwd),
                        "path_analysis": path_analysis,
                    }
                    decision = _agent_decision(invoker, policy_input)
                decisions.append(
                    {
                        "plan_id": plan_id,
                        "command_id": command_id,
                        **decision,
                        "path_analysis": path_analysis,
                    }
                )
    result = {
        "schema_version": 1,
        "kind": "harbor_fixer_execution_policy_decision",
        "policy_version": POLICY_VERSION,
        "status": (
            "allowed"
            if all(decision["decision"] == "allow" for decision in decisions)
            else "denied"
        ),
        "workspace_root": str(workspace_root.resolve()),
        "writable_roots": [str(root) for root in resolved_roots],
        "user_rules_path": rules_source,
        "user_rules": serialized_user_rules,
        "decisions": decisions,
    }
    write_json_atomic(output_dir / "execution-policy-decision.json", result)
    return result
