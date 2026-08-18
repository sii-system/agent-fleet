#!/usr/bin/env python3
"""Rollout-only worker maintenance helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
RESERVED_HEADERS = {"x-session-id", "proxy-x-session-id"}


def build_request_headers(raw: str, session_id: str = "") -> dict[str, str]:
    """Build trusted model-request headers from the versioned host config."""
    if not raw.strip():
        config: object = {"version": 1}
    else:
        config = json.loads(raw)
    if not isinstance(config, dict):
        raise TypeError("MODEL_REQUEST_CONFIG_JSON must be a JSON object")
    unknown = set(config) - {"version", "headers"}
    if unknown:
        raise ValueError(f"unsupported model request config fields: {sorted(unknown)}")
    if config.get("version") != 1:
        raise ValueError("MODEL_REQUEST_CONFIG_JSON version must be 1")

    header_config = config.get("headers", {})
    if not isinstance(header_config, dict):
        raise TypeError("model request config headers must be an object")
    unknown = set(header_config) - {"set"}
    if unknown:
        raise ValueError(f"unsupported header operations: {sorted(unknown)}")
    configured = header_config.get("set", {})
    if not isinstance(configured, dict):
        raise TypeError("model request config headers.set must be an object")

    headers: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in configured.items():
        if not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid model request header name: {name!r}")
        normalized_name = name.lower()
        if normalized_name in RESERVED_HEADERS:
            raise ValueError(f"model request header is reserved: {name!r}")
        if normalized_name in seen:
            raise ValueError(f"duplicate case-insensitive header: {name!r}")
        if not isinstance(value, str):
            raise TypeError(f"model request header {name!r} must be a string")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"model request header {name!r} contains control characters")
        seen.add(normalized_name)
        headers[name] = value

    if session_id:
        headers.update(
            {
                "X-Session-Id": session_id,
                "Proxy-X-Session-Id": session_id,
            }
        )
    return headers


def render_header_lines(existing: str, headers: dict[str, str]) -> str:
    """Merge headers into a newline-separated client header setting."""
    replaced = {name.lower() for name in headers}
    lines = [
        line
        for line in existing.splitlines()
        if line.strip()
        and line.partition(":")[0].strip().lower() not in replaced
    ]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    return "\n".join(lines)


def prune_trial_artifacts(worker_root: Path, keep: int) -> None:
    """Keep only the newest rollout trial directories for one worker."""
    keep = max(1, keep)
    try:
        trials = [path for path in worker_root.iterdir() if path.is_dir()]
    except FileNotFoundError:
        return
    trials.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in trials[keep:]:
        shutil.rmtree(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("prune-trials", "request-headers", "render-header-lines"),
    )
    parser.add_argument("path", nargs="?")
    parser.add_argument("--keep", type=int, default=20)
    args = parser.parse_args()

    if args.command == "request-headers":
        headers = build_request_headers(
            os.environ.get("MODEL_REQUEST_CONFIG_JSON", ""),
            os.environ.get("MODEL_REQUEST_SESSION_ID", ""),
        )
        print(json.dumps(headers, separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "render-header-lines":
        headers = json.loads(os.environ.get("MODEL_REQUEST_HEADERS_JSON", "{}"))
        print(
            render_header_lines(
                os.environ.get("TB_ANTHROPIC_CUSTOM_HEADERS", ""), headers
            )
        )
        return 0
    if not args.path:
        parser.error("prune-trials requires path")
    prune_trial_artifacts(Path(args.path), args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
