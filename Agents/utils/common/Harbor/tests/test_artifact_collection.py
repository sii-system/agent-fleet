from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
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

    async def test_local_pollers_are_bounded(self):
        tasks = []
        pollers = set()
        original_open = Path.open

        def count_open(path, *args, **kwargs):
            pollers.add(asyncio.current_task())
            return original_open(path, *args, **kwargs)

        async def download():
            async with artifact_download_slot():
                pass

        with patch.dict(os.environ, {"HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY": "2"}):
            try:
                async with AsyncExitStack() as owners:
                    for _ in range(2):
                        await owners.enter_async_context(artifact_download_slot())
                    with patch.object(Path, "open", count_open):
                        tasks = [asyncio.create_task(download()) for _ in range(64)]
                        await asyncio.sleep(0.15)
                        self.assertEqual(len(pollers), 2, "local filesystem pollers exceeded admission")
                        tasks[-1].cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await tasks[-1]
                        self.assertNotIn(tasks[-1], pollers)
                await asyncio.wait_for(asyncio.gather(*tasks[:-1]), 2)
                async with asyncio.timeout(1), artifact_download_slot(), artifact_download_slot():
                    pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_peer_process_progresses_before_local_backlog_drains(self):
        script = (
            "import asyncio\n"
            "from artifact_collection import artifact_download_slot\n"
            "async def main():\n"
            "    print('waiting', flush=True)\n"
            "    async with artifact_download_slot():\n"
            "        print('acquired', flush=True)\n"
            "asyncio.run(main())\n"
        )
        completed = 0
        tasks = []
        process = None

        async def download():
            nonlocal completed
            async with artifact_download_slot():
                await asyncio.sleep(0.025)
                completed += 1

        try:
            async with artifact_download_slot():
                tasks = [asyncio.create_task(download()) for _ in range(40)]
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(HARBOR_DIR)},
                )
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"waiting\n")
                await asyncio.sleep(0.025)
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"acquired\n")
            self.assertLess(completed, len(tasks), "local handoffs starved the peer until the queue drained")
            await asyncio.wait_for(process.wait(), 5)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    async def test_independent_directories_have_independent_admission(self):
        async with artifact_download_slot():
            with patch.dict(os.environ, {
                "HARBOR_ARTIFACT_LOCK_DIR": str(Path(self.directory.name) / "other"),
            }):
                async with asyncio.timeout(1), artifact_download_slot():
                    pass

    async def test_failed_lock_open_releases_admission(self):
        with (
            patch.object(Path, "open", side_effect=OSError("lock file unavailable")),
            self.assertRaisesRegex(OSError, "lock file unavailable"),
        ):
            async with artifact_download_slot():
                self.fail("lock acquisition unexpectedly succeeded")
        async with asyncio.timeout(1), artifact_download_slot():
            pass

    async def test_invalid_limit_rejected(self):
        with (
            patch.dict(os.environ, {"HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY": "0"}),
            self.assertRaises(ValueError),
        ):
            async with artifact_download_slot():
                self.fail("invalid slot limit accepted")
