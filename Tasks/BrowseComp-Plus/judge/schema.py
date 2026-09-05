"""Normalize upstream evaluator output into an RL-friendly result."""

from __future__ import annotations

import json
from pathlib import Path


def find_query_evaluation(eval_root: Path, query_id: str) -> tuple[Path, dict[str, object]]:
    candidates = sorted(eval_root.rglob(f"{query_id}_eval.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no evaluation found for query {query_id} below {eval_root}")
    path = candidates[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"evaluation is not an object: {path}")
    return path, payload


def scalar_result(eval_root: Path, query_id: str) -> dict[str, object]:
    path, payload = find_query_evaluation(eval_root, query_id)
    judge = payload.get("judge_result") or {}
    correct = bool(judge.get("correct")) if isinstance(judge, dict) else False
    return {
        "schema_version": 1,
        "benchmark": "browsecomp-plus",
        "query_id": query_id,
        "reward": 1.0 if correct else 0.0,
        "correct": correct,
        "evaluation_file": str(path),
        "judge_result": judge,
        "retrieval": payload.get("retrieval"),
        "citations": payload.get("citations"),
    }
