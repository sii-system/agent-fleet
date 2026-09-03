"""Runner-wide admission control for QZ Sandbox creation."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import random
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TextIO

QZ_DEFAULT_CREATE_CONCURRENCY = 10
_LOCK_POLL_INITIAL_SEC = 0.02
_LOCK_POLL_MAX_SEC = 0.25
_LOCK_POLL_JITTER_RATIO = 0.2


def qz_create_concurrency(environ: Mapping[str, str] = os.environ) -> int:
    """Return the operator-controlled create window, defaulting to 10."""
    raw = environ.get("QZ_CREATE_CONCURRENCY", "").strip()
    if not raw:
        return QZ_DEFAULT_CREATE_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("QZ_CREATE_CONCURRENCY must be a positive integer") from None
    if value <= 0:
        raise ValueError("QZ_CREATE_CONCURRENCY must be a positive integer")
    return value


def _runner_runtime_root(environ: Mapping[str, str]) -> Path:
    configured = (
        environ.get("XDG_RUNTIME_DIR", "").strip()
        or environ.get("AGENT_FLEET_RUNTIME_DIR", "").strip()
    )
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / f"agent-fleet-qz-{os.getuid()}"


def qz_create_lock_dir(
    api_url: str,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Return one runner-global lock directory without exposing credentials."""
    endpoint = api_url.strip().rstrip("/")
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:20]
    return _runner_runtime_root(environ) / "agent-fleet" / "qz-create-slots" / digest


def _try_lock_slot(path: Path) -> TextIO | None:
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _jittered_poll_delay(backoff_sec: float) -> float:
    spread = backoff_sec * _LOCK_POLL_JITTER_RATIO
    return random.uniform(backoff_sec - spread, backoff_sec + spread)


@asynccontextmanager
async def qz_create_slot(
    api_url: str,
    environ: Mapping[str, str] = os.environ,
) -> AsyncIterator[int]:
    """Acquire one rolling QZ create slot shared by local worker processes.

    The file descriptor remains open only while ``AsyncSandbox.create`` is in
    flight. ``flock`` releases automatically if a worker exits, and polling
    with non-blocking locks keeps Harbor's asyncio loop responsive.
    """
    concurrency = qz_create_concurrency(environ)
    lock_dir = qz_create_lock_dir(api_url, environ)
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle: TextIO | None = None
    slot = -1
    poll_backoff_sec = _LOCK_POLL_INITIAL_SEC
    try:
        while handle is None:
            for candidate in range(concurrency):
                candidate_handle = _try_lock_slot(
                    lock_dir / f"slot-{candidate:04d}.lock"
                )
                if candidate_handle is not None:
                    handle = candidate_handle
                    slot = candidate
                    break
            if handle is None:
                await asyncio.sleep(_jittered_poll_delay(poll_backoff_sec))
                poll_backoff_sec = min(
                    poll_backoff_sec * 2,
                    _LOCK_POLL_MAX_SEC,
                )
        yield slot
    finally:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
