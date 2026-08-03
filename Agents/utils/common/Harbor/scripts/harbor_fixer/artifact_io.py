"""Small JSON and text helpers for Fixer artifact files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .validation import ValidationError


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"expected JSON object: {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValidationError(f"missing JSONL file: {path}") from exc
    for line_number, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSONL record {path}:{line_number}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValidationError(f"expected JSON object at {path}:{line_number}")
        records.append(payload)
    return records


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_json(temp_path, payload)
    temp_path.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
