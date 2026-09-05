#!/usr/bin/env python3
"""Evaluate collected official-format BrowseComp run files host-side."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import default_source_root

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_DIR))
from judge.client import JudgeConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--eval-dir", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=default_source_root())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = JudgeConfig.from_env(args.source_root, args.ground_truth, args.eval_dir)
    if config.mode == "none":
        files = list(args.input_dir.glob("*.json"))
        completed = 0
        for path in files:
            try:
                completed += json.loads(path.read_text(encoding="utf-8")).get("status") == "completed"
            except (OSError, json.JSONDecodeError):
                pass
        payload = {"mode": "none", "evaluated": False, "runs": len(files), "completed": completed}
    else:
        config.evaluate(args.input_dir, args.force)
        summaries = sorted(args.eval_dir.rglob("evaluation_summary.json"))
        payload = {"mode": config.mode, "evaluated": True, "eval_dir": str(args.eval_dir.resolve()), "summaries": [str(path) for path in summaries]}
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True) if args.json else json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
