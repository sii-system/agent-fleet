#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

SUCCESS_VALUES = {"1", "1.0", "true", "success", "resolved", "pass", "passed"}


def _rows(path: Path) -> list[list[str]]:
    try:
        with path.open(encoding="utf-8") as handle:
            return [line.rstrip("\n").split("\t") for line in handle]
    except FileNotFoundError:
        return []


def reward_stats(done_path: Path) -> list[str]:
    counter = Counter(
        parts[2] or "none" for parts in _rows(done_path) if len(parts) >= 3
    )
    if not counter:
        return ["(none)"]
    return [
        f"reward={reward}: {count}"
        for reward, count in sorted(counter.items(), key=lambda item: (item[0] != "1.0", item[0]))
    ]


def success_stats(done_path: Path, failed_path: Path) -> list[str]:
    done_rows = [parts for parts in _rows(done_path) if len(parts) >= 3]
    success = sum(parts[2].strip().lower() in SUCCESS_VALUES for parts in done_rows)
    failed = sum(bool(parts and any(part.strip() for part in parts)) for parts in _rows(failed_path))
    finished = len(done_rows) + failed
    failure = finished - success
    rate = success / finished * 100.0 if finished else 0.0
    return [
        f"success:      {success}",
        f"fail:         {failure}",
        f"success_rate: {rate:.2f}%",
    ]


def exception_stats(done_path: Path, failed_path: Path) -> list[str]:
    counter: Counter[str] = Counter()
    for path in (done_path, failed_path):
        for parts in _rows(path):
            if len(parts) >= 4 and parts[3]:
                counter[parts[3]] += 1
            elif path == failed_path:
                counter["missing_result"] += 1
    if not counter:
        return ["(none)"]
    return [f"{name}: {count}" for name, count in counter.most_common(10)]


def environment_signal_stats(path: Path) -> list[str]:
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ["(none)"]
    counter = summary.get("monitor_environment_events_by_type") or {}
    if not counter:
        return ["(none)"]
    return [
        f"{name}: {count}"
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Harbor monitor statistics")
    parser.add_argument(
        "command",
        choices=("rewards", "success", "exceptions", "environment-signals"),
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    if args.command == "rewards":
        lines = reward_stats(args.paths[0])
    elif args.command == "success":
        lines = success_stats(args.paths[0], args.paths[1])
    elif args.command == "exceptions":
        lines = exception_stats(args.paths[0], args.paths[1])
    else:
        lines = environment_signal_stats(args.paths[0])
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
