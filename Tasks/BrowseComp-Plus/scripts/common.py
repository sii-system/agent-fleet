#!/usr/bin/env python3
"""Shared, dependency-free helpers for the BrowseComp-Plus integration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCHMARK_DIR.parents[1]
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def default_source_root() -> Path:
    configured = os.environ.get("BROWSECOMP_SOURCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "third_party" / "BrowseComp-Plus").resolve()


def default_cache_root() -> Path:
    configured = os.environ.get("BROWSECOMP_CACHE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    base = Path(os.environ.get("AGENT_FLEET_CACHE_DIR", Path.home() / ".cache" / "agent-fleet"))
    return (base / "browsecomp-plus").resolve()


def validate_task_id(value: object) -> str:
    task_id = str(value).strip()
    if task_id in {"", ".", ".."} or not SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError(f"unsafe BrowseComp query_id: {value!r}")
    return task_id


def load_questions(path: Path, *, require_answer: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError(f"{path}:{line_number}: expected a JSON object")
            task_id = validate_task_id(row.get("query_id"))
            query = row.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"{path}:{line_number}: query must be non-empty")
            if require_answer and not isinstance(row.get("answer"), str):
                raise ValueError(f"{path}:{line_number}: answer is required")
            if task_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate query_id {task_id}")
            seen.add(task_id)
            normalized = dict(row)
            normalized["query_id"] = task_id
            normalized["query"] = query.strip()
            rows.append(normalized)
    if not rows:
        raise ValueError(f"no questions found in {path}")
    return rows


def parse_selection(raw: str | None) -> list[str]:
    if not raw:
        return []
    selected: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        if not item.strip():
            continue
        task_id = validate_task_id(item)
        if task_id not in seen:
            selected.append(task_id)
            seen.add(task_id)
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def write_lines(path: Path, values: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
