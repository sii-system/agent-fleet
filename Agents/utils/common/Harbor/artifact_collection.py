"""Host-shared admission for artifact downloads by independent Harbor workers."""

from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path

_local_slots = weakref.WeakValueDictionary()


async def _acquire_download_lock(directory, limit):
    key = (asyncio.get_running_loop(), directory.resolve(), limit)
    slots = _local_slots.get(key)
    if slots is None:
        slots = asyncio.Semaphore(limit)
        _local_slots[key] = slots
    async with slots:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        while True:
            for index in range(limit):
                stream = (directory / f"download-{index}.lock").open("a")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                    continue
                except BaseException:
                    stream.close()
                    raise
                return stream
            await asyncio.sleep(0.1)


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
    stream = await _acquire_download_lock(directory, limit)
    try:
        yield
    finally:
        stream.close()
