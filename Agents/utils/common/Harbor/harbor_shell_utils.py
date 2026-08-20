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
    mounts = subparsers.add_parser("readonly-mounts")
    mounts.add_argument("--mount", nargs=3, action="append", default=[])
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
    else:
        specifications = [
            (Path(source), target, policy) for source, target, policy in args.mount
        ]
        print(json.dumps(readonly_mounts(specifications), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
