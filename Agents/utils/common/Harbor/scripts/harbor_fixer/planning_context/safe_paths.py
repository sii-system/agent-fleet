"""Shared, non-throwing path inspection for planning context collectors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def inspect_path(
    path: Path,
    *,
    expand_user: bool,
    include_writable: bool,
    include_executable: bool,
    include_mode: bool,
) -> dict[str, Any]:
    inspected = path.expanduser() if expand_user else path
    try:
        exists = inspected.exists()
        readable = os.access(inspected, os.R_OK)
        writable = os.access(inspected, os.W_OK) if include_writable else None
        executable = os.access(inspected, os.X_OK) if include_executable else None
    except OSError as exc:
        payload: dict[str, Any] = {
            "path": str(inspected),
            "status": "unavailable",
            "reason": f"path_unavailable:{exc.__class__.__name__}",
            "exists": False,
            "readable": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        if include_writable:
            payload["writable"] = False
        if include_executable:
            payload["executable"] = False
        return payload

    payload = {
        "path": str(inspected),
        "exists": exists,
        "readable": readable,
    }
    if include_writable:
        payload["writable"] = bool(writable)
    if include_executable:
        payload["executable"] = bool(executable)
    if not exists:
        return payload

    try:
        resolved = inspected.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        payload["status"] = "unavailable"
        payload["reason"] = f"path_unavailable:{exc.__class__.__name__}"
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
        return payload

    payload.update(
        {
            "realpath": str(resolved),
            "type": (
                "directory"
                if resolved.is_dir()
                else "file"
                if resolved.is_file()
                else "other"
            ),
            "size_bytes": info.st_size,
        }
    )
    if include_mode:
        payload["mode"] = oct(info.st_mode & 0o777)
    return payload
