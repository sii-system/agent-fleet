#!/usr/bin/env python3
"""Wait for a detached Harbor run, then collect and evaluate it."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import atomic_write_json

SCRIPT_DIR = Path(__file__).resolve().parent


def write_status(path: Path, state: str, **details: object) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **details,
        },
    )


def summary_is_current(path: Path, not_before_ns: int) -> bool:
    try:
        return path.is_file() and path.stat().st_mtime_ns >= not_before_ns
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary-file", required=True, type=Path)
    parser.add_argument("--summary-not-before-ns", required=True, type=int)
    parser.add_argument("--jobs-root", required=True, type=Path)
    parser.add_argument("--official-run-dir", required=True, type=Path)
    parser.add_argument("--task-manifest", required=True, type=Path)
    parser.add_argument("--eval-dir", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=172800)
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.summary_not_before_ns < 0:
        parser.error("--summary-not-before-ns must be nonnegative")

    deadline = time.monotonic() + args.timeout_seconds
    write_status(
        args.status_file,
        "waiting",
        summary_file=str(args.summary_file),
        summary_not_before_ns=args.summary_not_before_ns,
    )
    while not summary_is_current(args.summary_file, args.summary_not_before_ns):
        if time.monotonic() >= deadline:
            write_status(
                args.status_file,
                "timed_out",
                error=f"current Harbor summary did not appear within {args.timeout_seconds}s",
            )
            return 1
        time.sleep(args.poll_seconds)

    collect = [
        sys.executable,
        str(SCRIPT_DIR / "collect_results.py"),
        "--jobs-root",
        str(args.jobs_root),
        "--output-dir",
        str(args.official_run_dir),
        "--task-manifest",
        str(args.task_manifest),
    ]
    evaluate = [
        sys.executable,
        str(SCRIPT_DIR / "evaluate.py"),
        "--source-root",
        str(args.source_root),
        "--ground-truth",
        str(args.ground_truth),
        "--input-dir",
        str(args.official_run_dir),
        "--eval-dir",
        str(args.eval_dir),
    ]
    write_status(args.status_file, "collecting")
    try:
        subprocess.run(collect, check=True)
        write_status(args.status_file, "evaluating")
        subprocess.run(evaluate, check=True)
    except subprocess.CalledProcessError as exc:
        write_status(
            args.status_file,
            "failed",
            command=exc.cmd,
            returncode=exc.returncode,
        )
        return exc.returncode or 1
    write_status(
        args.status_file,
        "completed",
        official_run_dir=str(args.official_run_dir),
        eval_dir=str(args.eval_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
