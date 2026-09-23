"""Publish a completed benchmark summary once, independently of Analyzer."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
from pathlib import Path

from harbor_analyzer.io import write_text_atomic
from write_benchmark_summary import publish_benchmark_summary


def auto_summary(run_dir: Path, *, defer_analyzer: bool = False) -> bool:
    if os.environ.get("HARBOR_SUMMARY_ENABLED", "1") != "1":
        return False
    if os.environ.get("ROLLOUT", "0") == "1":
        return False
    if os.environ.get("HARBOR_FIXER_VERIFICATION_RERUN", "0") == "1":
        return False
    analyzer_enabled = os.environ.get("HARBOR_ANALYZER_ENABLED", "0") == "1"
    if defer_analyzer and analyzer_enabled:
        # The lifecycle supervisor drains opted-in analysis before publishing.
        return False
    if not (run_dir / "summary.txt").is_file():
        return False
    summary_dir = run_dir / "benchmark-summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    with (summary_dir / ".auto-summary.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = summary_dir / ".auto-summary-complete"
        raw_summary = run_dir / "summary.txt"
        generation = hashlib.sha256(raw_summary.read_bytes()).hexdigest()
        generation += f":{raw_summary.stat().st_mtime_ns}\n"
        if (marker.is_file() and marker.read_text(encoding="utf-8") == generation
                and (run_dir / "summary.md").is_file()):
            return False
        publish_benchmark_summary(
            run_dir, expected_run_id=os.environ.get("RUN_ID") or None,
            include_analyzer=analyzer_enabled, completed=True,
        )
        write_text_atomic(marker, generation)
    print(f"[RUN] benchmark summary: {run_dir / 'summary.md'}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--defer-analyzer", action="store_true")
    args = parser.parse_args()
    try:
        auto_summary(args.run_dir, defer_analyzer=args.defer_analyzer)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[WARN] automatic benchmark summary unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
