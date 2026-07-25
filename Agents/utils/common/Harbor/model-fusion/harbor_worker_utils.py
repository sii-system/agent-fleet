#!/usr/bin/env python3
"""Python helpers used only by Fleet's model-fusion Harbor glue."""

from __future__ import annotations

import argparse
import json


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("append-readonly-mounts",))
    parser.add_argument("path")
    parser.add_argument("--mount", nargs=2, action="append", default=[])
    args = parser.parse_args()

    return append_readonly_mounts(args.path, args.mount)


if __name__ == "__main__":
    raise SystemExit(main())
