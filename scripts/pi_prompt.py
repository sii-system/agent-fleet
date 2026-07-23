#!/usr/bin/env python3
"""Run the control-plane Pi translator and emit its final JSON object."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROVIDER = "sii-gateway"
API_KEY_ENV = "AGENT_FLEET_API_KEY"


class PromptFailure(RuntimeError):
    """A safe, user-facing Prompt translation failure."""


def normalized_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if base_url and not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PromptFailure("invalid BASE_URL for Pi provider")
    return base_url


def models_config(
    base_url: str,
    model: str,
    *,
    display_name: str = "Agent Fleet Prompt Translator",
) -> dict[str, Any]:
    """Keep this provider contract aligned with the merged Pi analyzer."""

    return {
        "providers": {
            PROVIDER: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": f"${API_KEY_ENV}",
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
        }
    }


def minimal_environment(runtime_dir: Path, api_key: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    passthrough = {
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
    for key in passthrough:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment.setdefault("PATH", os.defpath)
    environment["HOME"] = str(runtime_dir)
    environment["PI_CODING_AGENT_DIR"] = str(runtime_dir)
    environment["PI_OFFLINE"] = "1"
    environment[API_KEY_ENV] = api_key
    return environment


def parse_jsonl(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PromptFailure("Pi returned invalid JSONL") from exc
        if not isinstance(event, dict):
            raise PromptFailure("Pi returned a non-object JSONL event")
        events.append(event)
    if not events:
        raise PromptFailure("Pi returned no JSONL events")
    return events


def message_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def final_assistant_message(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate: dict[str, Any] | None = None
    for event in events:
        if event.get("type") not in {"message_end", "turn_end"}:
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            candidate = message
    return candidate


def provider_error(events: list[dict[str, Any]]) -> str:
    retry_errors = [
        str(event.get("finalError") or "")
        for event in events
        if event.get("type") == "auto_retry_end"
    ]
    retry_errors = [message for message in retry_errors if message]
    if retry_errors:
        return retry_errors[-1]
    message_errors = [
        str((event.get("message") or {}).get("errorMessage") or "")
        for event in events
        if isinstance(event.get("message"), dict)
    ]
    message_errors = [message for message in message_errors if message]
    return message_errors[-1] if message_errors else ""


def validate_event_stream(events: list[dict[str, Any]]) -> dict[str, Any]:
    session_ids = [
        str(event.get("id"))
        for event in events
        if event.get("type") == "session" and event.get("id")
    ]
    if len(session_ids) != 1:
        raise PromptFailure("Pi session lifecycle was not observed exactly once")

    agent_start = sum(event.get("type") == "agent_start" for event in events)
    agent_end = sum(event.get("type") == "agent_end" for event in events)
    if agent_start < 1 or agent_start != agent_end:
        raise PromptFailure("Pi agent lifecycle is incomplete")

    turn_start = sum(event.get("type") == "turn_start" for event in events)
    turn_end = sum(event.get("type") == "turn_end" for event in events)
    if turn_start < 1 or turn_start != turn_end:
        raise PromptFailure("Pi turn lifecycle is incomplete")

    error = provider_error(events)
    if error:
        raise PromptFailure(f"Pi provider request failed: {error}")

    message = final_assistant_message(events)
    if message is None:
        raise PromptFailure("Pi returned no final assistant message")
    stop_reason = str(message.get("stopReason") or "")
    if not stop_reason:
        raise PromptFailure("Pi final assistant message has no stop reason")
    if stop_reason != "stop":
        raise PromptFailure(f"Pi final assistant message stopped with {stop_reason}")

    text = message_text(message)
    if not text:
        raise PromptFailure("Pi returned an empty final assistant message")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PromptFailure("Pi final assistant message is not a JSON object") from exc
    if not isinstance(value, dict):
        raise PromptFailure("Pi final assistant message is not a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--system-prompt", required=True)
    parser.add_argument("--prompt", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    pi_binary = shutil.which(args.pi_bin)
    if pi_binary is None:
        raise PromptFailure(f"missing command: {args.pi_bin}; run scripts/setup.sh")
    base_url = normalized_base_url(args.base_url)
    model = args.model.strip()
    if not model:
        raise PromptFailure("MODEL is empty")
    api_key = os.environ.get(API_KEY_ENV, "")
    if not api_key:
        raise PromptFailure(f"missing API key environment: {API_KEY_ENV}")

    with tempfile.TemporaryDirectory(prefix="sii-fleet-prompt-") as temporary:
        root = Path(temporary)
        runtime_dir = root / "pi-agent"
        work_dir = root / "work"
        runtime_dir.mkdir()
        work_dir.mkdir()
        (runtime_dir / "models.json").write_text(
            json.dumps(models_config(base_url, model), indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            pi_binary,
            "--mode",
            "json",
            "--print",
            "--provider",
            PROVIDER,
            "--model",
            model,
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--system-prompt",
            args.system_prompt,
            # Pi print mode reads the user message from the trailing
            # positional argument, not stdin, like the Harbor analyzer.
            args.prompt,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=work_dir,
                env=minimal_environment(runtime_dir, api_key),
                stdin=subprocess.DEVNULL,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
            )
        except OSError as exc:
            raise PromptFailure(f"could not launch Pi: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise PromptFailure(f"Pi exited with code {completed.returncode}{suffix}")
        events = parse_jsonl(completed.stdout or "")
        return validate_event_stream(events)


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except PromptFailure as exc:
        message = str(exc)
        print(f"[ERROR] Prompt translation request failed: {message}", file=sys.stderr)
        if (
            ("UND_ERR_SOCKET" in message or "Unable to connect" in message)
            and any(
                os.environ.get(name)
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                )
            )
        ):
            print(
                "[HINT] If the model gateway is internal, add its hostname to NO_PROXY and retry.",
                file=sys.stderr,
            )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
