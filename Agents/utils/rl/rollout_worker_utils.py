#!/usr/bin/env python3
"""Rollout-only worker maintenance helpers."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


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
    parser.add_argument("command", choices=("prune-trials",))
    parser.add_argument("path")
    parser.add_argument("--keep", type=int, default=20)
    args = parser.parse_args()

    prune_trial_artifacts(Path(args.path), args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
