#!/usr/bin/env python3
"""Trusted host-side RL result processor for one BrowseComp Harbor trial."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from collect_results import collect_trial
from common import atomic_write_json, default_cache_root, default_source_root

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_DIR))
from judge.client import JudgeConfig  # noqa: E402
from judge.schema import scalar_result  # noqa: E402


def resolve_ground_truth() -> Path:
    configured = os.environ.get("BROWSECOMP_GROUND_TRUTH")
    if configured:
        return Path(configured).expanduser().resolve()
    return default_cache_root() / "private" / "browsecomp_plus_decrypted.jsonl"


def configure_managed_judge_python() -> None:
    if os.environ.get("BROWSECOMP_JUDGE_PYTHON"):
        return
    managed = default_cache_root() / "runtime" / "venv" / "bin" / "python"
    if managed.is_file():
        os.environ["BROWSECOMP_JUDGE_PYTHON"] = str(managed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source_root = Path(os.environ.get("BROWSECOMP_SOURCE_ROOT", default_source_root())).resolve()
    ground_truth = resolve_ground_truth()
    configure_managed_judge_python()
    work_root = args.output.parent / "browsecomp-plus"
    run_dir = work_root / "runs"
    eval_dir = work_root / "evals"
    item = collect_trial(args.result_file.resolve())
    if str(item["query_id"]) != args.task_id:
        raise ValueError(f"processor task mismatch: {item['query_id']} != {args.task_id}")
    atomic_write_json(run_dir / f"{args.task_id}.json", item)
    config = JudgeConfig.from_env(source_root, ground_truth, eval_dir)
    if config.mode == "none":
        result = {
            "schema_version": 1,
            "benchmark": "browsecomp-plus",
            "query_id": args.task_id,
            "reward": 0.0,
            "correct": False,
            "status": "unjudged",
            "message": "Set BROWSECOMP_JUDGE_MODE=local or openai for correctness rewards.",
        }
    else:
        config.evaluate(run_dir, force=True)
        result = scalar_result(eval_dir, args.task_id)
        result["status"] = "completed"
    result["official_run_file"] = str((run_dir / f"{args.task_id}.json").resolve())
    result["request_file"] = str(args.request_file.resolve())
    atomic_write_json(args.output.resolve(), result)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
