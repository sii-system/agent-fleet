"""Launch an isolated read-only Pi process for the configured analyzer model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor_pi_runtime import (
    PiProcessResult,
    normalized_base_url,
    run_pi_json_process,
    write_text_atomic,
)

PI_EXTENSION_PATH = Path(__file__).resolve().parent / "pi_extensions" / "analyzer_path_gate.ts"
ANALYZER_SYSTEM_PROMPT = (
    "You are a read-only Harbor failure analyzer. You may use only read-only file tools "
    "(read, grep, find, ls) to inspect the handover and referenced artifacts. Do not run "
    "shell commands, edit files, write files, repair tasks, restart workers, or stop a "
    "benchmark. Return exactly one JSON object."
)


@dataclass
class DispatchResult:
    report: dict[str, Any] | None
    provenance: dict[str, Any]
    block_reason: str | None
    stderr_tail: str


def _existing_paths(paths: list[Path | None]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for item in paths:
        if item is None:
            continue
        try:
            path = item.expanduser().resolve()
        except OSError:
            continue
        if not path.exists():
            continue
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        resolved.append(value)
    return resolved


def _analyzer_block_reason(reason: str | None) -> str | None:
    if reason == "pi_api_key_env_invalid":
        return "analyzer_api_key_env_invalid"
    if reason and reason.startswith("pi_api_key_env_missing:"):
        return reason.replace("pi_api_key_env_missing:", "analyzer_api_key_env_missing:", 1)
    if reason == "pi_base_url_invalid":
        return "analyzer_base_url_invalid"
    if reason == "pi_extension_missing":
        return "analyzer_path_gate_extension_missing"
    return reason


def _dispatch_result(result: PiProcessResult) -> DispatchResult:
    provenance = dict(result.provenance)
    provenance.pop("thinking_level", None)
    return DispatchResult(
        report=result.output_json,
        provenance=provenance,
        block_reason=_analyzer_block_reason(result.block_reason),
        stderr_tail=result.stderr_tail,
    )


def dispatch_to_child(
    *,
    prompt: str,
    analysis_id: str,
    output_dir: Path,
    pi_bin: str,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    agent_name: str,
    timeout_seconds: int,
    allowed_paths: list[Path | None] | None = None,
) -> DispatchResult:
    events_path = output_dir / "analyzer-subagent-events" / f"{analysis_id}.jsonl"
    runtime_home = output_dir / ".pi-analyzer-home" / analysis_id
    runtime_workdir = output_dir / ".pi-analyzer-work" / analysis_id
    access_audit_path = output_dir / "analyzer-tool-access" / f"{analysis_id}.jsonl"
    normalized_url = normalized_base_url(base_url)
    provenance: dict[str, Any] = {
        "launch_mode": "independent_pi_analyzer_subprocess",
        "pi_binary": pi_bin,
        "child_agent": agent_name,
        "provider": provider,
        "provider_api": "openai-completions",
        "provider_base_url": normalized_url,
        "api_key_env": api_key_env,
        "events_path": str(events_path),
        "tools_disabled": False,
        "builtin_tools_disabled": True,
        "tools_allowlist": ["read", "grep", "find", "ls"],
        "tool_access_mode": "path_gated_extension",
        "tool_access_audit_path": str(access_audit_path),
        "path_gate_extension": str(PI_EXTENSION_PATH),
        "extensions_disabled": False,
        "skills_disabled": True,
        "context_files_disabled": True,
        "independent_pi_process": True,
        "code_only_fallback_used": False,
    }

    runtime_workdir.mkdir(parents=True, exist_ok=True)
    access_audit_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_path_values = _existing_paths([runtime_workdir, *(allowed_paths or [])])
    write_text_atomic(
        runtime_workdir / "allowed-paths.json",
        json.dumps(
            {
                "analysis_id": analysis_id,
                "allowed_paths": allowed_path_values,
                "access_audit_path": str(access_audit_path),
                "note": "Pi analyzer tools are path-gated to these evidence paths; returned tool text is redacted.",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    extra_env = {
        "HARBOR_ANALYZER_ALLOWED_PATHS_JSON": json.dumps(
            allowed_path_values,
            ensure_ascii=False,
        ),
        "HARBOR_ANALYZER_ACCESS_AUDIT_PATH": str(access_audit_path),
    }
    result = run_pi_json_process(
        prompt=prompt,
        events_path=events_path,
        stderr_path=output_dir / "analyzer-subagent-stderr" / f"{analysis_id}.txt",
        runtime_home=runtime_home,
        runtime_workdir=runtime_workdir,
        pi_bin=pi_bin,
        provider=provider,
        model=model,
        base_url=normalized_url,
        api_key_env=api_key_env,
        agent_name=agent_name,
        display_name="Harbor Analyzer",
        timeout_seconds=timeout_seconds,
        launch_mode="independent_pi_analyzer_subprocess",
        system_prompt=ANALYZER_SYSTEM_PROMPT,
        provenance=provenance,
        no_proxy_env="HARBOR_ANALYZER_NO_PROXY",
        extra_env=extra_env,
        prompt_in_stdin=False,
        no_tools=False,
        no_builtin_tools=True,
        tools=["read", "grep", "find", "ls"],
        extension_path=PI_EXTENSION_PATH,
        disable_extensions=True,
        disable_skills=True,
        disable_prompt_templates=True,
        disable_context_files=True,
        stream_compaction=True,
        auth_header=True,
    )
    result.provenance["allowed_paths"] = allowed_path_values
    return _dispatch_result(result)
