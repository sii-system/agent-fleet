"""Resolve a Harbor run and print its benchmark summary."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from harbor_analyzer.io import load_json
from write_benchmark_summary import publish_benchmark_summary


def _run_start(run_dir: Path) -> tuple[float, str]:
    try:
        monitor = load_json(run_dir / "monitor" / "monitor-latest.json")
        started = float(monitor["evidence"]["run_start_ts"])
    except (OSError, ValueError, KeyError, TypeError):
        started = run_dir.stat().st_mtime
    return started, run_dir.name


def resolve_run(run: str | None, output_root: Path) -> Path:
    if run is not None:
        path = Path(run).expanduser()
        if not path.is_dir() and not path.is_absolute():
            path = output_root / path
        if not path.is_dir():
            raise ValueError(f"Run not found: {run}")
        return path.resolve()

    candidates = [
        path
        for path in output_root.glob("*")
        if path.is_dir()
        and (
            (path / "monitor").is_dir()
            or (path / "tasks.txt").is_file()
            or (path / "harbor-layout.kdl").is_file()
        )
    ]
    if not candidates:
        raise ValueError(
            f"No Harbor runs found in {output_root}. "
            "Provide a run directory if it was saved elsewhere."
        )
    return max(candidates, key=_run_start).resolve()


def summarize_run(run_dir: Path) -> str:
    monitor_path = run_dir / "monitor" / "monitor-latest.json"
    if not monitor_path.is_file():
        raise ValueError(f"Run {run_dir.name} has no monitoring results to summarize yet.")
    monitor = load_json(monitor_path)
    if monitor.get("benchmark_status") == "running":
        raise ValueError(
            f"Run {run_dir.name} is still reported as running. "
            "Retry after benchmark shutdown and analysis finish."
        )
    output_path = run_dir / "summary.md"
    publish_benchmark_summary(run_dir, expected_run_id=run_dir.name)
    return output_path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_fleet.sh summary",
        description="Generate and print a summary of the latest Harbor run.",
    )
    parser.add_argument(
        "run", nargs="?", metavar="RUN",
        help="run ID or directory; defaults to the latest run under OUTPUT_ROOT",
    )
    args = parser.parse_args()
    output_root = Path(os.environ["OUTPUT_ROOT"]).expanduser()
    try:
        run_dir = resolve_run(args.run, output_root)
        print(f"Summarizing run: {run_dir}", file=sys.stderr)
        print(summarize_run(run_dir), end="")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Summary unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
