from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))
from artifact_collection import artifact_download_slot  # noqa: E402


class ArtifactCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = patch.dict(os.environ, {
            "HARBOR_ARTIFACT_LOCK_DIR": self.directory.name,
            "HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY": "1",
        })
        self.settings.start()

    async def asyncTearDown(self):
        self.settings.stop()
        self.directory.cleanup()

    async def test_independent_process_waits_and_death_releases_slot(self):
        script = (
            "import asyncio\n"
            "from artifact_collection import artifact_download_slot\n"
            "async def main():\n"
            "    async with artifact_download_slot():\n"
            "        print('acquired', flush=True)\n"
            "        await asyncio.sleep(60)\n"
            "asyncio.run(main())\n"
        )
        process = None
        try:
            async with artifact_download_slot():
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(HARBOR_DIR)},
                )
                read = asyncio.create_task(process.stdout.readline())
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(read), 0.3)
            self.assertEqual(await asyncio.wait_for(read, 5), b"acquired\n")
            process.kill()
            await process.wait()
            async with asyncio.timeout(5), artifact_download_slot():
                pass
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    async def test_cancellation_releases_owned_slot(self):
        entered = asyncio.Event()

        async def owner():
            async with artifact_download_slot():
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1), artifact_download_slot():
            pass

    async def test_invalid_limit_rejected(self):
        with (
            patch.dict(os.environ, {"HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY": "0"}),
            self.assertRaises(ValueError),
        ):
            async with artifact_download_slot():
                self.fail("invalid slot limit accepted")
