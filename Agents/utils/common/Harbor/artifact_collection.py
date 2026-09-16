"""Host-shared admission for artifact downloads by independent Harbor workers."""

from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def artifact_download_slot():
    """Acquire one advisory-lock slot; process death releases the slot too.

    Workers sharing this directory must use the same concurrency setting. Keep
    the lock files in place: unlinking a held lock would allow another owner.
    """
    limit = int(os.environ.get("HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY", "4"))
    if limit < 1:
        raise ValueError("HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY must be positive")
    directory = Path(os.environ.get(
        "HARBOR_ARTIFACT_LOCK_DIR",
        str(Path(tempfile.gettempdir()) / f"agent-fleet-artifacts-{os.getuid()}"),
    ))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        for index in range(limit):
            stream = (directory / f"download-{index}.lock").open("a")
            try:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                yield
                return
            finally:
                stream.close()
        await asyncio.sleep(0.1)
