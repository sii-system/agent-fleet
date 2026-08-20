"""Small JSON and text helpers for Fixer artifact files."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from secrets import token_hex
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
    def _create_temp_path() -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            candidate = path.with_name(
                f".{path.name}.{os.getpid()}.{token_hex(8)}.tmp"
            )
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                return candidate
            except Exception:
                with suppress(FileNotFoundError):
                    Path(candidate).unlink()
                raise
        raise FileExistsError("could not allocate a temporary artifact file")

    temp_path = _create_temp_path()
    try:
        temp_path.replace(path)
    except Exception:
        with suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(8)}.tmp")
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except Exception:
        with suppress(FileNotFoundError):
            temp_path.unlink()
        raise
