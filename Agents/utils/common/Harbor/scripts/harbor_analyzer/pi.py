"""Launch an isolated read-only Pi process for the configured analyzer model."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

from .io import canonical_json, write_text_atomic

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MESSAGE_UPDATE_RE = re.compile(br'^\s*\{\s*"type"\s*:\s*"message_update"\s*[,}]')
PI_EXTENSION_PATH = Path(__file__).resolve().parent / "pi_extensions" / "analyzer_path_gate.ts"


@dataclass
class DispatchResult:
    report: dict[str, Any] | None
    provenance: dict[str, Any]
    block_reason: str | None
    stderr_tail: str


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalized_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if value and not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


def _reason_code(message: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", message.strip().lower()).strip("_")
    return value[:80] or "unknown_error"


def _pi_environment(base_url: str, runtime_home: Path, api_key_env: str) -> tuple[dict[str, str], bool]:
    environment: dict[str, str] = {}
    passthrough_keys = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
    for key in passthrough_keys:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment.setdefault("PATH", os.defpath)
    environment["HOME"] = str(runtime_home)
    environment["PI_CODING_AGENT_DIR"] = str(runtime_home)
    environment["PI_OFFLINE"] = "1"
    if os.environ.get(api_key_env):
        environment[api_key_env] = os.environ[api_key_env]
    if not _enabled(os.environ.get("HARBOR_ANALYZER_NO_PROXY")):
        return environment, False

    hostname = urlparse(base_url).hostname
    if not hostname:
        return environment, False
    for key in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in environment.get(key, "").split(",") if item.strip()]
        if hostname not in entries:
            entries.append(hostname)
        environment[key] = ",".join(entries)
    return environment, True


def _models_config(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
) -> dict[str, Any]:
    return {
        "providers": {
            provider: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": f"${api_key_env}",
                "authHeader": True,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsUsageInStreaming": True,
                    "maxTokensField": "max_tokens",
                    "thinkingFormat": "zai",
                },
                "models": [
                    {
                        "id": model,
                        "name": "Harbor Analyzer",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 204800,
                        "maxTokens": 32768,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }


def _message_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


@dataclass
class _EventStreamState:
    stored_sha256: Any = field(default_factory=hashlib.sha256)
    session_ids: list[str] = field(default_factory=list)
    jsonl_event_count: int = 0
    jsonl_invalid_line_count: int = 0
    agent_start_count: int = 0
    agent_end_count: int = 0
    turn_start_count: int = 0
    turn_end_count: int = 0
    tool_event_count: int = 0
    retry_start_count: int = 0
    retry_end_count: int = 0
    message_updates_dropped: int = 0
    observed_bytes: int = 0
    stored_bytes: int = 0
    final_retry_error: str = ""
    last_provider_error: str = ""
    final_message: dict[str, Any] | None = None
    stream_error: OSError | None = None

    def observe(self, event: dict[str, Any]) -> None:
        self.jsonl_event_count += 1
        event_type = str(event.get("type") or "")
        if event_type == "session" and event.get("id"):
            self.session_ids.append(str(event["id"]))
        elif event_type == "agent_start":
            self.agent_start_count += 1
        elif event_type == "agent_end":
            self.agent_end_count += 1
        elif event_type == "turn_start":
            self.turn_start_count += 1
        elif event_type == "turn_end":
            self.turn_end_count += 1
        elif event_type == "auto_retry_start":
            self.retry_start_count += 1
        elif event_type == "auto_retry_end":
            self.retry_end_count += 1
            final_error = str(event.get("finalError") or "")
            if final_error:
                self.final_retry_error = final_error
        if event_type.startswith("tool_execution_"):
            self.tool_event_count += 1

        message = event.get("message")
        if isinstance(message, dict):
            provider_error = str(message.get("errorMessage") or "")
            if provider_error:
                self.last_provider_error = provider_error
            if event_type in {"message_end", "turn_end"} and message.get("role") == "assistant":
                self.final_message = message

    @property
    def final_text(self) -> str:
        return _message_text(self.final_message)

    @property
    def provider_error(self) -> str:
        return self.final_retry_error or self.last_provider_error

    @property
    def final_stop_reason(self) -> str:
        return str((self.final_message or {}).get("stopReason") or "")


def _message_update_line(raw_line: bytes) -> bool:
    return MESSAGE_UPDATE_RE.match(raw_line) is not None


def _consume_event_stream(
    stream: BinaryIO,
    output: BinaryIO,
    state: _EventStreamState,
) -> None:
    try:
        for raw_line in iter(stream.readline, b""):
            if not raw_line.strip():
                continue
            state.observed_bytes += len(raw_line)
            if _message_update_line(raw_line):
                state.jsonl_event_count += 1
                state.message_updates_dropped += 1
                continue

            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                state.jsonl_invalid_line_count += 1
            else:
                if isinstance(event, dict):
                    state.observe(event)
                else:
                    state.jsonl_invalid_line_count += 1

            if state.stream_error is not None:
                continue
            try:
                output.write(raw_line)
                output.flush()
            except OSError as exc:
                state.stream_error = exc
                continue
            state.stored_sha256.update(raw_line)
            state.stored_bytes += len(raw_line)
    except OSError as exc:
        state.stream_error = exc


def _update_event_provenance(
    provenance: dict[str, Any],
    state: _EventStreamState,
) -> None:
    provenance.update(
        {
            "events_sha256": state.stored_sha256.hexdigest(),
            "jsonl_event_count": state.jsonl_event_count,
            "jsonl_invalid_line_count": state.jsonl_invalid_line_count,
            "agent_start_count": state.agent_start_count,
            "agent_end_count": state.agent_end_count,
            "turn_start_count": state.turn_start_count,
            "turn_end_count": state.turn_end_count,
            "tool_event_count": state.tool_event_count,
            "auto_retry_start_count": state.retry_start_count,
            "auto_retry_end_count": state.retry_end_count,
            "message_updates_dropped": state.message_updates_dropped,
            "events_observed_bytes": state.observed_bytes,
            "events_stored_bytes": state.stored_bytes,
        }
    )


def _loads_final_json(text: str) -> tuple[dict[str, Any] | None, bool]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value, False

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value, True
    return None, False


def load_final_json_from_event_stream(path: Path) -> dict[str, Any] | None:
    state = _EventStreamState()
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                if not isinstance(event, dict):
                    return None
                state.observe(event)
    except OSError:
        return None
    report, _ = _loads_final_json(state.final_text)
    if report is None:
        return None
    return report


def load_report_from_event_stream(path: Path) -> dict[str, Any] | None:
    """Backward-compatible alias for callers that only need the final JSON."""

    return load_final_json_from_event_stream(path)


def _pi_version(binary: str, environment: dict[str, str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            cwd=cwd,
            env=environment,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (completed.stdout or "").strip()
    return value or None


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
    normalized_base_url = _normalized_base_url(base_url)
    provenance: dict[str, Any] = {
        "launch_mode": "independent_pi_analyzer_subprocess",
        "pi_binary": pi_bin,
        "child_agent": agent_name,
        "provider": provider,
        "provider_api": "openai-completions",
        "provider_base_url": normalized_base_url,
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

    resolved_pi = shutil.which(pi_bin)
    if resolved_pi is None:
        return DispatchResult(None, provenance, "pi_binary_not_found", "")
    if not provider.strip():
        return DispatchResult(None, provenance, "pi_provider_not_configured", "")
    if not model.strip():
        return DispatchResult(None, provenance, "pi_model_not_configured", "")
    if not ENV_NAME_RE.fullmatch(api_key_env):
        return DispatchResult(None, provenance, "analyzer_api_key_env_invalid", "")
    if not os.environ.get(api_key_env):
        return DispatchResult(None, provenance, f"analyzer_api_key_env_missing:{api_key_env}", "")
    parsed_url = urlparse(normalized_base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return DispatchResult(None, provenance, "analyzer_base_url_invalid", "")
    if not PI_EXTENSION_PATH.is_file():
        return DispatchResult(None, provenance, "analyzer_path_gate_extension_missing", "")

    models_config = _models_config(
        provider=provider,
        model=model,
        base_url=normalized_base_url,
        api_key_env=api_key_env,
    )
    models_text = canonical_json(models_config) + "\n"
    runtime_home.mkdir(parents=True, exist_ok=True)
    runtime_workdir.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    access_audit_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(runtime_home / "models.json", models_text)
    environment, proxy_bypassed = _pi_environment(normalized_base_url, runtime_home, api_key_env)
    allowed_path_values = _existing_paths([runtime_workdir, *(allowed_paths or [])])
    environment["HARBOR_ANALYZER_ALLOWED_PATHS_JSON"] = json.dumps(
        allowed_path_values,
        ensure_ascii=False,
    )
    environment["HARBOR_ANALYZER_ACCESS_AUDIT_PATH"] = str(access_audit_path)
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
    provenance.update(
        {
            "pi_binary_resolved": resolved_pi,
            "pi_version": _pi_version(resolved_pi, environment, runtime_workdir),
            "child_model": model,
            "runtime_pi_home": str(runtime_home),
            "runtime_workdir": str(runtime_workdir),
            "models_config_sha256": hashlib.sha256(models_text.encode("utf-8")).hexdigest(),
            "provider_proxy_bypassed": proxy_bypassed,
            "environment_mode": "minimal",
            "environment_keys": sorted(environment),
            "allowed_paths": allowed_path_values,
        }
    )

    command = [
        resolved_pi,
        "--mode",
        "json",
        "--print",
        "--provider",
        provider,
        "--model",
        model,
        "--no-session",
        "--no-builtin-tools",
        "--tools",
        "read,grep,find,ls",
        "--extension",
        str(PI_EXTENSION_PATH),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--system-prompt",
        (
            "You are a read-only Harbor failure analyzer. You may use only read-only file tools "
            "(read, grep, find, ls) to inspect the handover and referenced artifacts. Do not run "
            "shell commands, edit files, write files, repair tasks, restart workers, or stop a "
            "benchmark. Return exactly one JSON object."
        ),
        prompt,
    ]
    stderr_path = output_dir / "analyzer-subagent-stderr" / f"{analysis_id}.txt"
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    provenance["stderr_path"] = str(stderr_path)
    provenance["timeout_seconds"] = timeout_seconds
    state = _EventStreamState()
    timed_out = False
    try:
        with events_path.open("wb") as events_file, stderr_path.open(
            "w",
            encoding="utf-8",
        ) as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=runtime_workdir,
                env=environment,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                start_new_session=True,
            )
            assert process.stdout is not None
            reader = threading.Thread(
                target=_consume_event_stream,
                args=(process.stdout, events_file, state),
            )
            reader.start()
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                return_code = process.wait()
            reader.join()
            process.stdout.close()
            stderr_file.flush()
    except OSError as exc:
        return DispatchResult(None, provenance, f"pi_dispatch_os_error:{exc}", "")

    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    provenance["pi_exit_code"] = return_code
    provenance["pi_session_ids"] = state.session_ids
    _update_event_provenance(provenance, state)
    if timed_out:
        provenance["events_partial"] = True
        return DispatchResult(None, provenance, "pi_dispatch_timeout", stderr[-4000:])
    if state.stream_error is not None:
        return DispatchResult(
            None,
            provenance,
            f"pi_event_stream_os_error:{state.stream_error}",
            stderr[-4000:],
        )
    if return_code != 0:
        return DispatchResult(
            None,
            provenance,
            f"pi_dispatch_exit_code:{return_code}",
            stderr[-4000:],
        )

    if state.jsonl_invalid_line_count:
        return DispatchResult(None, provenance, "pi_jsonl_invalid", stderr[-4000:])
    if len(state.session_ids) != 1:
        return DispatchResult(None, provenance, "pi_subagent_session_not_observed", stderr[-4000:])
    if state.agent_start_count < 1 or state.agent_start_count != state.agent_end_count:
        return DispatchResult(None, provenance, "pi_subagent_lifecycle_invalid", stderr[-4000:])
    if state.turn_start_count < 1 or state.turn_start_count != state.turn_end_count:
        return DispatchResult(None, provenance, "pi_subagent_turn_invalid", stderr[-4000:])
    provider_error = state.provider_error
    output_block_reason = (
        f"pi_provider_request_failed:{_reason_code(provider_error)}"
        if provider_error
        else "pi_final_message_truncated"
        if state.final_stop_reason == "length"
        else None
    )
    final_stop_reason = state.final_stop_reason
    if final_stop_reason:
        provenance["pi_final_stop_reason"] = final_stop_reason
    final_text = state.final_text
    if not final_text and output_block_reason:
        if provider_error:
            provenance["pi_provider_final_error"] = provider_error
        return DispatchResult(None, provenance, output_block_reason, stderr[-4000:])
    report, extracted_from_text = _loads_final_json(final_text)
    if report is None:
        if output_block_reason:
            if provider_error:
                provenance["pi_provider_final_error"] = provider_error
            return DispatchResult(None, provenance, output_block_reason, stderr[-4000:])
        if final_text:
            provenance["final_message_sha256"] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        return DispatchResult(None, provenance, "pi_final_message_invalid_json", stderr[-4000:])

    provenance["final_message_sha256"] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    provenance["final_json_extracted_from_text"] = extracted_from_text
    provenance["provenance_valid"] = True
    return DispatchResult(report, provenance, None, stderr[-4000:])
