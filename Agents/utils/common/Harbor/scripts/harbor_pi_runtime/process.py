"""Launch isolated Pi subprocesses and validate their JSON event streams."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MESSAGE_UPDATE_RE = re.compile(br'^\s*\{\s*"type"\s*:\s*"message_update"\s*[,}]')
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
PERSISTED_EVENT_TYPES = {
    "agent_end",
    "agent_start",
    "auto_retry_end",
    "auto_retry_start",
    "message_end",
    "result",
    "session",
    "turn_end",
    "turn_start",
}
@dataclass
class PiProcessResult:
    output_json: dict[str, Any] | None
    output_text: str
    provenance: dict[str, Any]
    block_reason: str | None
    stderr_tail: str


class _StreamingEventState:
    def __init__(self) -> None:
        self.hasher = hashlib.sha256()
        self.event_count = 0
        self.invalid_lines = 0
        self.message_updates_dropped = 0
        self.observed_bytes = 0
        self.stored_bytes = 0
        self.stream_error: OSError | None = None


def _consume_compact_stream(
    stream: BinaryIO,
    output: BinaryIO,
    state: _StreamingEventState,
) -> None:
    try:
        for raw_line in iter(stream.readline, b""):
            if not raw_line.strip():
                continue
            state.observed_bytes += len(raw_line)
            if MESSAGE_UPDATE_RE.match(raw_line):
                state.event_count += 1
                state.message_updates_dropped += 1
                continue
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                state.invalid_lines += 1
            else:
                if isinstance(event, dict):
                    state.event_count += 1
                else:
                    state.invalid_lines += 1
            if state.stream_error is not None:
                continue
            try:
                output.write(raw_line)
                output.flush()
            except OSError as exc:
                state.stream_error = exc
                continue
            state.hasher.update(raw_line)
            state.stored_bytes += len(raw_line)
    except OSError as exc:
        state.stream_error = exc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if value and not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


def reason_code(message: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", message.strip().lower()).strip("_")
    return value[:80] or "unknown_error"


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def pi_environment(
    *,
    base_url: str,
    runtime_home: Path,
    api_key_env: str,
    no_proxy_env: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], bool]:
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
    if extra_env:
        environment.update(extra_env)

    if no_proxy_env is None or not _enabled(os.environ.get(no_proxy_env)):
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


def models_config(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    display_name: str,
    auth_header: bool | None = None,
) -> dict[str, Any]:
    provider_config: dict[str, Any] = {
        "baseUrl": base_url,
        "api": "openai-completions",
        "apiKey": f"${api_key_env}",
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
                "name": display_name,
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
    if auth_header is not None:
        provider_config["authHeader"] = auth_header
    return {"providers": {provider: provider_config}}


def parse_jsonl(raw: str) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    invalid_lines = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            invalid_lines += 1
    return events, invalid_lines


def compact_jsonl_event_stream(
    raw_path: Path,
    compact_path: Path,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Persist final/audit events while discarding cumulative streaming updates."""

    raw_hasher = hashlib.sha256()
    kept_events: list[dict[str, Any]] = []
    invalid_lines = 0
    raw_event_count = 0
    discarded_counts: dict[str, int] = {}
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("rb") as source, compact_path.open("w", encoding="utf-8") as target:
        for raw_line in source:
            raw_hasher.update(raw_line)
            decoded = raw_line.decode("utf-8", errors="replace")
            if not decoded.strip():
                continue
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                invalid_lines += 1
                target.write(decoded if decoded.endswith("\n") else f"{decoded}\n")
                continue
            if not isinstance(event, dict):
                invalid_lines += 1
                target.write(decoded if decoded.endswith("\n") else f"{decoded}\n")
                continue
            raw_event_count += 1
            event_type = str(event.get("type") or "")
            persist = (
                event_type in PERSISTED_EVENT_TYPES
                or event_type.startswith("tool_execution_")
                or "error" in event_type
            )
            if persist:
                kept_events.append(event)
                target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                discarded_counts[event_type or "unknown"] = (
                    discarded_counts.get(event_type or "unknown", 0) + 1
                )
    compact_sha256 = hashlib.sha256(compact_path.read_bytes()).hexdigest()
    return kept_events, invalid_lines, {
        "raw_events_sha256": raw_hasher.hexdigest(),
        "raw_jsonl_event_count": raw_event_count,
        "persisted_jsonl_event_count": len(kept_events),
        "discarded_event_counts": dict(sorted(discarded_counts.items())),
        "events_sha256": compact_sha256,
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


def final_assistant_text(events: list[dict[str, Any]]) -> str:
    return _message_text(final_assistant_message(events))


def final_assistant_message(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate: dict[str, Any] | None = None
    for event in events:
        if event.get("type") not in {"message_end", "turn_end"}:
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            candidate = message
    return candidate


def _provider_error_message(
    events: list[dict[str, Any]],
    retry_end_events: list[dict[str, Any]],
) -> str:
    if retry_end_events:
        final_retry_error = str(retry_end_events[-1].get("finalError") or "")
        if final_retry_error:
            return final_retry_error
    error_messages = [
        str((event.get("message") or {}).get("errorMessage") or "")
        for event in events
        if isinstance(event.get("message"), dict)
    ]
    error_messages = [message for message in error_messages if message]
    return error_messages[-1] if error_messages else ""


def final_output_block_reason(
    events: list[dict[str, Any]],
    retry_end_events: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    provider_error = _provider_error_message(events, retry_end_events)
    if provider_error:
        return f"pi_provider_request_failed:{reason_code(provider_error)}", provider_error
    final_message = final_assistant_message(events)
    stop_reason = str((final_message or {}).get("stopReason") or "")
    if stop_reason == "length":
        return "pi_final_message_truncated", None
    return None, None


def loads_final_json(text: str) -> tuple[dict[str, Any] | None, bool]:
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
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    events, invalid_lines = parse_jsonl(raw)
    if invalid_lines:
        return None
    report, _ = loads_final_json(final_assistant_text(events))
    return report


def pi_version(binary: str, environment: dict[str, str], cwd: Path) -> str | None:
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


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _process_start_ticks(pid: int) -> int | None:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(
            ")", 1
        )[1].split()
        return int(stat_fields[19])
    except (IndexError, OSError, ValueError):
        return None


def _write_process_record(path: Path, process: subprocess.Popen[Any]) -> None:
    start_ticks = _process_start_ticks(process.pid)
    if start_ticks is None:
        raise OSError("cannot identify the Pi process")
    write_text_atomic(
        path,
        json.dumps(
            {"status": "running", "pid": process.pid, "start_ticks": start_ticks},
            sort_keys=True,
        )
        + "\n",
    )


def _record_or_kill_process(path: Path, process: subprocess.Popen[Any]) -> None:
    try:
        _write_process_record(path, process)
    except OSError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()
        raise


def _read_event_stream(path: Path) -> tuple[list[dict[str, Any]], int]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return parse_jsonl(raw)


def _run_streaming_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    events_path: Path,
    stderr_path: Path,
    prompt: str | None,
    timeout_seconds: int,
    process_record_path: Path | None,
) -> tuple[int, bool, _StreamingEventState]:
    state = _StreamingEventState()
    timed_out = False
    with events_path.open("wb") as events_file, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            shell=False,
            stdin=subprocess.PIPE if prompt is not None else None,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            start_new_session=True,
        )
        if process_record_path is not None:
            _record_or_kill_process(process_record_path, process)
        assert process.stdout is not None
        reader = threading.Thread(
            target=_consume_compact_stream,
            args=(process.stdout, events_file, state),
        )
        reader.start()
        if prompt is not None:
            assert process.stdin is not None
            process.stdin.write(prompt.encode())
            process.stdin.close()
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
    return return_code, timed_out, state


def run_pi_json_process(
    *,
    prompt: str,
    events_path: Path,
    stderr_path: Path,
    runtime_home: Path,
    runtime_workdir: Path,
    pi_bin: str,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    agent_name: str,
    display_name: str,
    timeout_seconds: int,
    launch_mode: str,
    system_prompt: str,
    provenance: dict[str, Any] | None = None,
    no_proxy_env: str | None = None,
    extra_env: dict[str, str] | None = None,
    prompt_in_stdin: bool = False,
    no_tools: bool = False,
    no_builtin_tools: bool = True,
    tools: list[str] | None = None,
    extension_path: Path | None = None,
    thinking_level: str | None = None,
    disable_extensions: bool = True,
    disable_skills: bool = True,
    disable_prompt_templates: bool = True,
    disable_context_files: bool = True,
    stream_compaction: bool = False,
    auth_header: bool | None = None,
    process_record_path: Path | None = None,
) -> PiProcessResult:
    normalized_url = normalized_base_url(base_url)
    record: dict[str, Any] = {
        **(provenance or {}),
        "launch_mode": launch_mode,
        "pi_binary": pi_bin,
        "child_agent": agent_name,
        "provider": provider,
        "provider_api": "openai-completions",
        "provider_base_url": normalized_url,
        "api_key_env": api_key_env,
        "events_path": str(events_path),
        "independent_pi_process": True,
    }

    if prompt_in_stdin and stream_compaction:
        return PiProcessResult(None, "", record, "pi_streaming_stdin_unsupported", "")
    resolved_pi = shutil.which(pi_bin)
    if resolved_pi is None:
        return PiProcessResult(None, "", record, "pi_binary_not_found", "")
    if not provider.strip():
        return PiProcessResult(None, "", record, "pi_provider_not_configured", "")
    if not model.strip():
        return PiProcessResult(None, "", record, "pi_model_not_configured", "")
    if not ENV_NAME_RE.fullmatch(api_key_env):
        return PiProcessResult(None, "", record, "pi_api_key_env_invalid", "")
    if thinking_level is not None and thinking_level not in THINKING_LEVELS:
        return PiProcessResult(None, "", record, "pi_thinking_level_invalid", "")
    if not os.environ.get(api_key_env):
        return PiProcessResult(None, "", record, f"pi_api_key_env_missing:{api_key_env}", "")
    parsed_url = urlparse(normalized_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return PiProcessResult(None, "", record, "pi_base_url_invalid", "")
    if extension_path is not None and not extension_path.is_file():
        return PiProcessResult(None, "", record, "pi_extension_missing", "")

    runtime_home.mkdir(parents=True, exist_ok=True)
    runtime_workdir.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    models_text = canonical_json(
        models_config(
            provider=provider,
            model=model,
            base_url=normalized_url,
            api_key_env=api_key_env,
            display_name=display_name,
            auth_header=auth_header,
        )
    ) + "\n"
    write_text_atomic(runtime_home / "models.json", models_text)
    environment, proxy_bypassed = pi_environment(
        base_url=normalized_url,
        runtime_home=runtime_home,
        api_key_env=api_key_env,
        no_proxy_env=no_proxy_env,
        extra_env=extra_env,
    )
    record.update(
        {
            "pi_binary_resolved": resolved_pi,
            "pi_version": pi_version(resolved_pi, environment, runtime_workdir),
            "child_model": model,
            "runtime_pi_home": str(runtime_home),
            "runtime_workdir": str(runtime_workdir),
            "models_config_sha256": hashlib.sha256(models_text.encode("utf-8")).hexdigest(),
            "provider_proxy_bypassed": proxy_bypassed,
            "environment_mode": "minimal",
            "environment_keys": sorted(environment),
            "stderr_path": str(stderr_path),
            "timeout_seconds": timeout_seconds,
            "thinking_level": thinking_level or "default",
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
    ]
    if thinking_level is not None:
        command.extend(["--thinking", thinking_level])
    if no_tools:
        command.append("--no-tools")
    if no_builtin_tools:
        command.append("--no-builtin-tools")
    if tools is not None:
        command.extend(["--tools", ",".join(tools)])
    if extension_path is not None:
        command.extend(["--extension", str(extension_path)])
    if disable_extensions:
        command.append("--no-extensions")
    if disable_skills:
        command.append("--no-skills")
    if disable_prompt_templates:
        command.append("--no-prompt-templates")
    if disable_context_files:
        command.append("--no-context-files")
    command.extend(["--system-prompt", system_prompt])
    if not prompt_in_stdin:
        command.append(prompt)

    raw_events_path = (
        events_path if stream_compaction else events_path.with_name(f".{events_path.name}.raw")
    )
    stream_state: _StreamingEventState | None = None
    try:
        if stream_compaction:
            return_code, timed_out, stream_state = _run_streaming_process(
                command,
                cwd=runtime_workdir,
                environment=environment,
                events_path=events_path,
                stderr_path=stderr_path,
                prompt=prompt if prompt_in_stdin else None,
                timeout_seconds=timeout_seconds,
                process_record_path=process_record_path,
            )
        else:
            with raw_events_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w",
                encoding="utf-8",
            ) as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=runtime_workdir,
                    env=environment,
                    shell=False,
                    text=True,
                    stdin=subprocess.PIPE if prompt_in_stdin else None,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                if process_record_path is not None:
                    _record_or_kill_process(process_record_path, process)
                try:
                    process.communicate(
                        input=prompt if prompt_in_stdin else None,
                        timeout=timeout_seconds,
                    )
                    return_code = process.returncode
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    return_code = process.wait()
                    stdout_file.flush()
                    stderr_file.flush()
                    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                    record["pi_exit_code"] = return_code
                    record["events_partial"] = True
                    _, _, event_record = compact_jsonl_event_stream(
                        raw_events_path,
                        events_path,
                    )
                    raw_events_path.unlink(missing_ok=True)
                    record.update(event_record)
                    return PiProcessResult(None, "", record, "pi_dispatch_timeout", stderr[-4000:])
    except OSError as exc:
        if not stream_compaction:
            record["raw_events_path"] = str(raw_events_path)
        return PiProcessResult(None, "", record, f"pi_dispatch_os_error:{exc}", "")

    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    record["pi_exit_code"] = return_code
    if stream_compaction and timed_out:
        record["events_partial"] = True
    try:
        if stream_compaction:
            assert stream_state is not None
            events, invalid_lines = _read_event_stream(events_path)
            event_record = {
                "raw_jsonl_event_count": stream_state.event_count,
                "persisted_jsonl_event_count": len(events),
                "discarded_event_counts": {
                    "message_update": stream_state.message_updates_dropped,
                },
                "message_updates_dropped": stream_state.message_updates_dropped,
                "events_observed_bytes": stream_state.observed_bytes,
                "events_stored_bytes": stream_state.stored_bytes,
                "events_sha256": stream_state.hasher.hexdigest(),
            }
        else:
            events, invalid_lines, event_record = compact_jsonl_event_stream(
                raw_events_path,
                events_path,
            )
        record.update(event_record)
        if not stream_compaction:
            raw_events_path.unlink(missing_ok=True)
    except OSError as exc:
        if not stream_compaction:
            record["raw_events_path"] = str(raw_events_path)
        return PiProcessResult(
            None,
            "",
            record,
            f"pi_event_compaction_error:{exc}",
            stderr[-4000:],
        )
    session_ids = [
        str(event.get("id"))
        for event in events
        if event.get("type") == "session" and event.get("id")
    ]
    agent_start_count = sum(event.get("type") == "agent_start" for event in events)
    agent_end_count = sum(event.get("type") == "agent_end" for event in events)
    turn_start_count = sum(event.get("type") == "turn_start" for event in events)
    turn_end_count = sum(event.get("type") == "turn_end" for event in events)
    retry_start_count = sum(event.get("type") == "auto_retry_start" for event in events)
    retry_end_events = [event for event in events if event.get("type") == "auto_retry_end"]
    tool_event_count = sum(str(event.get("type") or "").startswith("tool_execution_") for event in events)
    record.update(
        {
            "pi_session_ids": session_ids,
            "jsonl_event_count": event_record["raw_jsonl_event_count"],
            "jsonl_invalid_line_count": invalid_lines,
            "agent_start_count": agent_start_count,
            "agent_end_count": agent_end_count,
            "turn_start_count": turn_start_count,
            "turn_end_count": turn_end_count,
            "tool_event_count": tool_event_count,
            "auto_retry_start_count": retry_start_count,
            "auto_retry_end_count": len(retry_end_events),
        }
    )
    record["persisted_jsonl_event_count"] = len(events)
    if stream_state is not None and stream_state.stream_error is not None and not timed_out:
        return PiProcessResult(
            None,
            "",
            record,
            f"pi_event_stream_os_error:{stream_state.stream_error}",
            stderr[-4000:],
        )
    if stream_compaction and timed_out:
        return PiProcessResult(None, "", record, "pi_dispatch_timeout", stderr[-4000:])
    if return_code != 0:
        return PiProcessResult(None, "", record, f"pi_dispatch_exit_code:{return_code}", stderr[-4000:])
    if invalid_lines:
        return PiProcessResult(None, "", record, "pi_jsonl_invalid", stderr[-4000:])
    if len(session_ids) != 1:
        return PiProcessResult(None, "", record, "pi_subagent_session_not_observed", stderr[-4000:])
    if agent_start_count < 1 or agent_start_count != agent_end_count:
        return PiProcessResult(None, "", record, "pi_subagent_lifecycle_invalid", stderr[-4000:])
    if turn_start_count < 1 or turn_start_count != turn_end_count:
        return PiProcessResult(None, "", record, "pi_subagent_turn_invalid", stderr[-4000:])

    output_block_reason, provider_error = final_output_block_reason(events, retry_end_events)
    final_message = final_assistant_message(events)
    final_stop_reason = str((final_message or {}).get("stopReason") or "")
    if final_stop_reason:
        record["pi_final_stop_reason"] = final_stop_reason
    final_text = final_assistant_text(events)
    output_json, extracted_from_text = (
        loads_final_json(final_text) if final_text else (None, False)
    )
    if output_json is None:
        if output_block_reason:
            if provider_error:
                record["pi_provider_final_error"] = provider_error
            return PiProcessResult(None, final_text, record, output_block_reason, stderr[-4000:])
        if final_text:
            record["final_message_sha256"] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        return PiProcessResult(None, final_text, record, "pi_final_message_invalid_json", stderr[-4000:])

    record["final_message_sha256"] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    record["final_json_extracted_from_text"] = extracted_from_text
    record["provenance_valid"] = True
    return PiProcessResult(output_json, final_text, record, None, stderr[-4000:])
