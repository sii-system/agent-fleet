#!/usr/bin/env python3
"""Python helpers used only by Fleet's model-fusion Harbor glue."""

from __future__ import annotations

import argparse
import json
import shlex


def append_readonly_mounts(
    mounts_json: str, mounts: list[tuple[str, str]]
) -> int:
    try:
        payload = json.loads(mounts_json)
    except json.JSONDecodeError:
        return 2
    if not isinstance(payload, list):
        return 2
    for source, target in mounts:
        payload.append(
            {
                "type": "bind",
                "source": source,
                "target": target,
                "read_only": True,
            }
        )
    print(json.dumps(payload, ensure_ascii=True))
    return 0


def redact_command(argv: list[str]) -> list[str]:
    """Redact secret-bearing Harbor arguments before rendering a command."""
    redacted: list[str] = []
    redact_env = False
    redact_value = False
    for argument in argv:
        if redact_value:
            redacted.append("<redacted>")
            redact_value = False
            continue
        if redact_env:
            key, separator, _value = argument.partition("=")
            secret_key = any(
                marker in key.upper()
                for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
            )
            redacted.append(
                f"{key}=<redacted>" if separator and secret_key else argument
            )
            redact_env = False
            continue

        redacted.append(argument)
        if argument in ("--ae", "--ve"):
            redact_env = True
        elif argument in ("--api-key", "--auth-token", "--token", "--password"):
            redact_value = True
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    mounts_parser = subparsers.add_parser("append-readonly-mounts")
    mounts_parser.add_argument("path")
    mounts_parser.add_argument("--mount", nargs=2, action="append", default=[])
    render_parser = subparsers.add_parser("render-command")
    render_parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "append-readonly-mounts":
        return append_readonly_mounts(args.path, args.mount)
    print(shlex.join(redact_command(args.argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
