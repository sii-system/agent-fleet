#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse


def online_event(
    phase: str,
    component: str,
    event: str,
    severity: str,
    fatal: bool,
    message: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if environ is None else environ
    task_index = values.get("HARBOR_TASK_INDEX", "")
    payload = {
        "schema": 1,
        "task_id": int(task_index) if task_index.isdigit() else None,
        "task_name": values.get("HARBOR_TASK_ID", ""),
        "phase": phase,
        "component": component,
        "event": event,
        "severity": severity,
        "fatal": fatal,
        "scope": "task",
        "message": message,
    }
    return "[ONLINE_ENV] " + json.dumps(payload, separators=(",", ":"))


def normalize_json(raw: str) -> str:
    return json.dumps(json.loads(raw), separators=(",", ":"))


def json_string_field(raw: str, field: str) -> str:
    value = json.loads(raw).get(field, "")
    return value if isinstance(value, str) else ""


def url_hostname(value: str) -> str:
    return urlparse(value).hostname or ""


def check_http_preflight(
    service: str, status: str, *, auth_status: str = "", timeout: str = "",
    strict: bool = True, environ: Mapping[str, str] | None = None,
) -> int:
    """Classify responses after Shell performs its existing curl requests."""
    values = os.environ if environ is None else environ
    endpoint = values.get("OPIK_URL_OVERRIDE", "").rstrip("/")
    if service == "opik-ingestion":
        url = f"{endpoint}/v1/private/spans/batch"
        if status.startswith("2") or status in {"400", "401", "403", "422"}:
            return 0
        if status in {"404", "405"}:
            print(f"[ERROR] Opik ingestion endpoint returned {status}: {url}", file=sys.stderr)
            print("[ERROR] this usually means the API prefix is wrong (expected .../api).", file=sys.stderr)
            return 1
        print(f"[WARN] Opik ingestion preflight returned HTTP {status} for: {url}", file=sys.stderr)
    elif service == "opik-projects":
        if not (status.startswith("2") or status in {"401", "403"}):
            print(f"[ERROR] Opik project preflight returned HTTP {status}", file=sys.stderr)
            print(f"[ERROR] endpoint: {endpoint}/v1/private/projects", file=sys.stderr)
            return 1
    elif service == "dockerhub":
        registry_failed = status not in {"200", "401"}
        auth_failed = auth_status != "200"
        if registry_failed:
            print(f"[WARN] cannot reach https://registry-1.docker.io/v2/ within {timeout}s "
                  f"(status={status or '000'})", file=sys.stderr)
        if auth_failed:
            print(f"[WARN] cannot reach https://auth.docker.io token service within {timeout}s "
                  f"(status={auth_status or '000'})", file=sys.stderr)
        if registry_failed or auth_failed:
            message = ("Docker Hub preflight failed in strict mode" if strict else
                       "Docker Hub preflight failed; continuing because strict mode is disabled")
            if values.get("HARBOR_ONLINE_ANALYSIS") == "1":
                print(online_event(
                    "preflight", "docker_registry",
                    "connectivity_unavailable" if strict else "connectivity_degraded",
                    "critical" if strict else "warning", strict, message, values,
                ))
            if strict:
                print("[ERROR] Docker Hub preflight failed in strict mode.", file=sys.stderr)
                print("[ERROR] fix network/proxy/registry mirror first, or set "
                      "HARBOR_DOCKERHUB_PREFLIGHT_STRICT=0 / HARBOR_SKIP_DOCKERHUB_PREFLIGHT=1.",
                      file=sys.stderr)
                return 1
            print("[WARN] Docker Hub preflight failed, continuing (strict mode disabled).", file=sys.stderr)
            print("[WARN] if image pull fails later, set a proxy/registry mirror and retry.", file=sys.stderr)
    else:
        raise ValueError(f"unknown preflight service: {service}")
    return 0


class OpenCodeBaseURLRequired(ValueError):
    """The selected provider needs an explicit endpoint for its exception."""


def agent_model_host(environ: Mapping[str, str] | None = None) -> str:
    """Return only the configured active model host, never URL credentials."""
    values = os.environ if environ is None else environ
    agent = values.get("AGENT", "")
    base_url = values.get("HARBOR_ANTHROPIC_BASE_URL", "")
    if agent == "dsh-sdk-minimal":
        base_url = values.get("BASE_URL", "")
    elif agent == "opencode":
        raw = values.get("OPENCODE_CONFIG_CONTENT", "")
        provider = values.get("HARBOR_MODEL", "").partition("/")[0]
        config = json.loads(raw) if raw else {}
        options = config.get("provider", {}).get(provider, {}).get("options", {})
        # Built-in providers can ignore ANTHROPIC_BASE_URL. Require an explicit
        # selected-provider endpoint instead of inferring another gateway.
        if "baseURL" not in options:
            raise OpenCodeBaseURLRequired
        base_url = options["baseURL"]
    elif agent == "pi":
        raw = values.get("PI_MODELS_CONFIG", "")
        if raw:
            model_provider, separator, _ = values.get("HARBOR_MODEL", "").partition("/")
            # AgentFleetPi.run selects the provider from its qualified model.
            provider = model_provider if separator else values.get("PI_PROVIDER", "")
            config = json.loads(raw).get("providers", {}).get(provider, {})
            base_url = config.get("baseUrl", base_url)
    elif agent != "claude-code":
        return ""
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        # Empty gateway settings can render a relative /v1 provider URL.
        # Endpoint validation belongs to preflight; there is no host to add.
        return ""
    return parsed.hostname


def native_task_config(root: Path, task_file: Path, limit: str = "") -> dict:
    names = [name.strip() for name in task_file.read_text().splitlines() if name.strip()]
    if limit:
        if int(limit) < 1:
            raise ValueError("HARBOR_LIMIT must be positive")
        names = names[:int(limit)]
    if not names:
        raise ValueError("native task selection is empty")
    root = root.resolve()
    tasks = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not (path / "task.toml").is_file():
            raise ValueError(f"not a Harbor task under {root}: {name}")
        tasks.append({"path": str(path)})
    return {"tasks": tasks}


def readonly_mounts(
    specifications: Sequence[tuple[Path, str, str]],
) -> list[dict[str, object]]:
    mounts: list[dict[str, object]] = []
    for source, target, policy in specifications:
        if policy == "exists" and not source.exists():
            continue
        if policy == "uv-bin" and not (
            source.is_dir() and (source / "uv").exists() and (source / "uvx").exists()
        ):
            continue
        if policy not in {"always", "exists", "uv-bin"}:
            raise ValueError(f"unsupported mount policy: {policy}")
        mounts.append(
            {
                "type": "bind",
                "source": str(source),
                "target": target,
                "read_only": True,
            }
        )
    return mounts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Python helpers for Harbor shell entrypoints")
    subparsers = parser.add_subparsers(dest="command", required=True)
    event_parser = subparsers.add_parser("online-event")
    event_parser.add_argument("phase")
    event_parser.add_argument("component")
    event_parser.add_argument("event")
    event_parser.add_argument("severity")
    event_parser.add_argument("fatal", choices=("true", "false"))
    event_parser.add_argument("message")
    normalize = subparsers.add_parser("normalize-json")
    normalize.add_argument("raw")
    field = subparsers.add_parser("json-string-field")
    field.add_argument("raw")
    field.add_argument("field")
    hostname = subparsers.add_parser("url-hostname")
    hostname.add_argument("url")
    http = subparsers.add_parser("check-http")
    http.add_argument("service", choices=("opik-ingestion", "opik-projects", "dockerhub"))
    http.add_argument("status")
    http.add_argument("--auth-status", default="")
    http.add_argument("--timeout", default="")
    http.add_argument("--strict", default="1")
    subparsers.add_parser("agent-model-host")
    mounts = subparsers.add_parser("readonly-mounts")
    mounts.add_argument("--mount", nargs=3, action="append", default=[])
    native = subparsers.add_parser("native-task-config")
    native.add_argument("root", type=Path)
    native.add_argument("task_file", type=Path)
    native.add_argument("limit", nargs="?", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "online-event":
        print(
            online_event(
                args.phase,
                args.component,
                args.event,
                args.severity,
                args.fatal == "true",
                args.message,
            )
        )
    elif args.command == "normalize-json":
        try:
            print(normalize_json(args.raw))
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"INVALID_JSON::{exc}", file=sys.stderr)
            return 1
    elif args.command == "json-string-field":
        print(json_string_field(args.raw, args.field))
    elif args.command == "url-hostname":
        print(url_hostname(args.url))
    elif args.command == "check-http":
        return check_http_preflight(
            args.service, args.status, auth_status=args.auth_status,
            timeout=args.timeout, strict=args.strict == "1",
        )
    elif args.command == "agent-model-host":
        try:
            print(agent_model_host())
        except OpenCodeBaseURLRequired:
            print("Set options.baseURL for the selected OpenCode provider", file=sys.stderr)
            return 1
        except (ValueError, TypeError, AttributeError):
            print("Invalid model gateway configuration", file=sys.stderr)
            return 1
    elif args.command == "native-task-config":
        print(json.dumps(native_task_config(args.root, args.task_file, args.limit)))
    else:
        specifications = [
            (Path(source), target, policy) for source, target, policy in args.mount
        ]
        print(json.dumps(readonly_mounts(specifications), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
