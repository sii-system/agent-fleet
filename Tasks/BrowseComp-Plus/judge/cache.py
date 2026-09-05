"""Dependency-free cache identity helpers for BrowseComp evaluations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

EVALUATION_CACHE_SCHEMA_VERSION = 1


def evaluation_fingerprint(
    *,
    run: dict[str, Any],
    prompt: str,
    relevant_docids: set[str],
    model: str,
    base_url: str | None,
    api_mode: str,
    max_output_tokens: int,
) -> str:
    """Identify every input that can affect a cached evaluation."""

    payload = {
        "schema_version": EVALUATION_CACHE_SCHEMA_VERSION,
        "run": run,
        "grader_prompt": prompt,
        "relevant_docids": sorted(relevant_docids),
        "judge": {
            "model": model,
            "base_url": base_url or "",
            "api_mode": api_mode,
            "max_output_tokens": max_output_tokens,
        },
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_cached_evaluation(
    path: Path, expected_fingerprint: str
) -> dict[str, Any] | None:
    """Return a cache entry only when its schema and full identity match."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    cache = payload.get("evaluation_cache")
    if not isinstance(cache, dict):
        return None
    if cache.get("schema_version") != EVALUATION_CACHE_SCHEMA_VERSION:
        return None
    if cache.get("fingerprint") != expected_fingerprint:
        return None
    return payload
