"""Regressions for exported links and ownership of cancelled archive workers."""

import asyncio
import io
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.dsec_controller import transfer


class DsecExportLinkTests(unittest.TestCase):
    def test_links_escaping_exported_subtree_are_rejected_before_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runner-secret").write_text("host-only")
            target = root / "output"
            target.mkdir()
            (target / "keep").write_text("existing output")
            for linkname, alias_target in (
                ("../runner-secret", "."), ("alias", "../runner-secret"),
                ("../agent/runner-secret", "."),
            ):
                with self.subTest(linkname=linkname):
                    archive = root / "input.tar"
                    with tarfile.open(archive, "w") as tar:
                        entry = tarfile.TarInfo("agent")
                        entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                        tar.addfile(entry)
                        entry = tarfile.TarInfo("agent/runner-secret")
                        entry.size = 7
                        tar.addfile(entry, io.BytesIO(b"payload"))
                        link = tarfile.TarInfo("agent/log.txt")
                        link.type, link.linkname = tarfile.SYMTYPE, linkname
                        tar.addfile(link)
                        alias = tarfile.TarInfo("agent/alias")
                        alias.type, alias.linkname = tarfile.SYMTYPE, alias_target
                        tar.addfile(alias)
                    with self.assertRaisesRegex(ValueError, "exported subtree"):
                        transfer._extract_directory(archive, target, source_name="agent")
                    self.assertEqual([p.name for p in target.iterdir()], ["keep"])
                    self.assertEqual((root / "runner-secret").read_text(), "host-only")

    def test_links_within_exported_subtree_remain_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "input.tar"
            with tarfile.open(archive, "w") as tar:
                entry = tarfile.TarInfo("agent/data")
                entry.size, entry.mode = 7, 0o644
                tar.addfile(entry, io.BytesIO(b"payload"))
                for name, destination in (("first", "data"), ("second", "first")):
                    link = tarfile.TarInfo("agent/" + name)
                    link.type, link.linkname = tarfile.SYMTYPE, destination
                    tar.addfile(link)
            transfer._extract_directory(archive, root / "output", source_name="agent")
            self.assertTrue((root / "output/second").is_symlink())
            self.assertEqual((root / "output/second").read_bytes(), b"payload")

    def test_existing_output_link_cannot_redirect_merge_to_host_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runner-secret").write_bytes(b"host-only")
            target = root / "output"
            target.mkdir()
            (target / "nested").symlink_to("..", target_is_directory=True)
            archive = root / "input.tar"
            with tarfile.open(archive, "w") as tar:
                entry = tarfile.TarInfo("agent/nested/runner-secret")
                entry.size = 7
                tar.addfile(entry, io.BytesIO(b"replace"))
            with self.assertRaisesRegex(ValueError, "collection directory"):
                transfer._extract_directory(archive, target, source_name="agent")
            self.assertEqual((root / "runner-secret").read_bytes(), b"host-only")


class DsecWorkerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_extraction_finishes_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            copytree = transfer.shutil.copytree

            def blocked_copy(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release copy worker")
                try:
                    return copytree(*args, **kwargs)
                finally:
                    finished.set()

            async def download(_sandbox, _source, archive, _limit):
                with tarfile.open(archive, "w") as tar:
                    entry = tarfile.TarInfo("agent/output")
                    entry.size = 7
                    tar.addfile(entry, io.BytesIO(b"payload"))

            environment = SimpleNamespace(_client=SimpleNamespace(download_archive=download),
                                          _sandbox_id="test", _transfer_max_bytes=1024)
            with patch.object(transfer.shutil, "copytree", blocked_copy):
                operation = asyncio.create_task(transfer.DsecTransferMixin.download_dir(
                    environment, "/logs/agent", root / "output"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    for _ in range(2):
                        operation.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(operation.done())
                    self.assertFalse((root / "output").exists())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertTrue(finished.is_set())
                self.assertEqual((root / "output/output").read_bytes(), b"payload")

    async def test_cancelled_packing_keeps_archive_until_worker_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "data").write_bytes(b"payload")
            entered, release = threading.Event(), threading.Event()
            archives = []
            pack = transfer._pack_directory

            def blocked_pack(source, archive, *args, **kwargs):
                archive.write_bytes(b"owned temporary archive")
                archives.append(archive)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release packing worker")
                self.assertTrue(archive.exists())
                return pack(source, archive, *args, **kwargs)

            environment = SimpleNamespace(_client=SimpleNamespace(upload_archive=AsyncMock()),
                                          _sandbox_id="test", _transfer_max_bytes=1 << 20)
            with patch.object(transfer, "_pack_directory", blocked_pack):
                operation = asyncio.create_task(transfer.DsecTransferMixin.upload_dir(
                    environment, source, "/input"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    operation.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(operation.done())
                    self.assertTrue(archives[0].exists())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
            self.assertFalse(archives[0].exists())
            environment._client.upload_archive.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
