"""Fixer agent invocation contract and Pi-backed implementation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harbor_pi_runtime import run_pi_json_process, write_text_atomic

FIXER_INPUT_MARKER = "HARBOR_FIXER_INPUT_JSON:"
FIXER_SYSTEM_PROMPT = (
    "You are a Harbor Fixer Pi coding agent. Return exactly one JSON object. "
    "Do not execute commands, edit files, read external files, start subagents, or use tools. "
    "Use only the prompt instructions and the JSON payload supplied in this turn."
)


class AgentInvoker(Protocol):
    def invoke(self, prompt: str, payload: dict[str, Any], *, attempt: int, label: str) -> str:
        """Return raw agent output."""


@dataclass(frozen=True)
class PiInvocationConfig:
    pi_bin: str = "pi"
    provider: str = "harbor-fixer"
    model: str = ""
    base_url: str = ""
    api_key_env: str = "HARBOR_FIXER_API_KEY"
    timeout_seconds: int = 900
    thinking_level: str | None = None


def _safe_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if cleaned == value and cleaned and len(cleaned) <= 120:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned[:100] or 'agent'}-{digest}"


def _compose_prompt(prompt: str, payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            prompt.rstrip(),
            "",
            "Read the following Harbor Fixer input JSON and return JSON only.",
            FIXER_INPUT_MARKER,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "",
        ]
    )


class PiAgentInvoker:
    """Run one isolated no-session Pi coding-agent subprocess per agent call."""

    def __init__(self, output_dir: Path, config: PiInvocationConfig) -> None:
        self.output_dir = output_dir
        self.config = config

    @property
    def configured_pi_binary(self) -> str:
        return self.config.pi_bin

    def invoke(self, prompt: str, payload: dict[str, Any], *, attempt: int, label: str) -> str:
        safe_label = _safe_path_component(label)
        attempt_name = f"attempt-{attempt}"
        call_id = f"{safe_label}-{attempt_name}"
        rendered_prompt = _compose_prompt(prompt, payload)
        prompt_path = self.output_dir / "pi-agent-prompts" / safe_label / f"{attempt_name}.txt"
        write_text_atomic(prompt_path, rendered_prompt)
        process_record_path = self.output_dir / "active-agent-processes" / f"{call_id}.json"
        write_text_atomic(process_record_path, '{"status":"launching"}\n')
        try:
            result = run_pi_json_process(
                prompt=rendered_prompt,
                events_path=self.output_dir / "pi-agent-events" / safe_label / f"{attempt_name}.jsonl",
                stderr_path=self.output_dir / "pi-agent-stderr" / safe_label / f"{attempt_name}.txt",
                runtime_home=self.output_dir / ".pi-fixer-home" / call_id,
                runtime_workdir=self.output_dir / ".pi-fixer-work" / call_id,
                pi_bin=self.config.pi_bin,
                provider=self.config.provider,
                model=self.config.model,
                base_url=self.config.base_url,
                api_key_env=self.config.api_key_env,
                agent_name="harbor_fixer_pi_agent",
                display_name="Harbor Fixer",
                timeout_seconds=self.config.timeout_seconds,
                launch_mode="independent_pi_fixer_subprocess",
                system_prompt=FIXER_SYSTEM_PROMPT,
                provenance={
                    "attempt": attempt,
                    "label": label,
                    "prompt_path": str(prompt_path),
                    "tools_disabled": True,
                    "builtin_tools_disabled": True,
                    "tools_allowlist": [],
                    "extensions_disabled": True,
                    "skills_disabled": True,
                    "context_files_disabled": True,
                },
                no_proxy_env="HARBOR_FIXER_NO_PROXY",
                prompt_in_stdin=True,
                no_tools=True,
                no_builtin_tools=True,
                tools=None,
                thinking_level=self.config.thinking_level,
                disable_extensions=True,
                disable_skills=True,
                disable_prompt_templates=True,
                disable_context_files=True,
                process_record_path=process_record_path,
            )
        finally:
            process_record_path.unlink(missing_ok=True)
        provenance_path = self.output_dir / "pi-agent-provenance" / safe_label / f"{attempt_name}.json"
        write_text_atomic(
            provenance_path,
            json.dumps(result.provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if result.block_reason or result.output_json is None:
            error = result.block_reason or "pi_final_json_missing"
            if result.stderr_tail:
                error = f"{error}: {result.stderr_tail}"
            raise RuntimeError(f"pi agent failed for {label} attempt {attempt}: {error}")
        return json.dumps(result.output_json, ensure_ascii=False)
