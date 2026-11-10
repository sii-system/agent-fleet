"""Validate Moby root-export names without weakening ordinary archive checks."""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.dsec_controller import transfer


def write_root_archive(path):
    """Mirror names verified with Moby TarResourceRebaseOpts('.', '/')."""
    with tarfile.open(path, "w") as archive:
        for name in ("/", "/etc"):
            member = tarfile.TarInfo(name)
            member.type, member.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(member)
        member = tarfile.TarInfo("/etc/input")
        member.size, member.mode = 7, 0o444
        archive.addfile(member, io.BytesIO(b"payload"))
        member = tarfile.TarInfo("/etc/input-copy")
        member.type, member.linkname, member.mode = tarfile.LNKTYPE, "/etc/input", 0o444
        archive.addfile(member)
        member = tarfile.TarInfo("/link")
        member.type, member.linkname = tarfile.SYMTYPE, "etc/input"
        archive.addfile(member)


class DsecRootDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_root_rebases_native_names_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def download(sandbox, source, archive, maximum):
                self.assertEqual((sandbox, source), ("test", "/"))
                write_root_archive(archive)

            environment = SimpleNamespace(_client=SimpleNamespace(download_archive=download),
                                          _sandbox_id="test", _transfer_max_bytes=1 << 20)
            target = root / "output"
            await transfer.DsecTransferMixin.download_dir(environment, "/", target)
            self.assertEqual((target / "etc/input").read_bytes(), b"payload")
            self.assertEqual((target / "etc/input-copy").read_bytes(), b"payload")
            self.assertEqual((target / "etc/input").stat().st_mode & 0o777, 0o444)
            self.assertTrue((target / "link").is_symlink())
            self.assertEqual((target / "link").read_bytes(), b"payload")

    def test_ordinary_downloads_still_reject_absolute_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "input.tar"
            write_root_archive(archive)
            with self.assertRaisesRegex(ValueError, "unsafe member path"):
                transfer._extract_directory(archive, root / "output", source_name="agent")
            self.assertFalse((root / "output").exists())

    def test_root_rebasing_preserves_path_and_link_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").write_bytes(b"host-only")
            target = root / "output"
            target.mkdir()
            (target / "keep").write_bytes(b"existing")
            for name, kind, linkname in (
                ("/../outside", tarfile.REGTYPE, ""),
                ("//host/file", tarfile.REGTYPE, ""),
                ("/link", tarfile.SYMTYPE, "/etc/passwd"),
                ("/link", tarfile.LNKTYPE, "/../outside"),
            ):
                with self.subTest(name=name, kind=kind, linkname=linkname):
                    archive = root / "input.tar"
                    with tarfile.open(archive, "w") as output:
                        member = tarfile.TarInfo(name)
                        member.type, member.linkname = kind, linkname
                        output.addfile(member)
                    with self.assertRaises(ValueError):
                        transfer._extract_directory(archive, target, container_root=True)
                    self.assertEqual([p.name for p in target.iterdir()], ["keep"])
                    self.assertEqual((root / "outside").read_bytes(), b"host-only")

    def test_root_download_keeps_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "input.tar"
            write_root_archive(archive)
            with self.assertRaisesRegex(ValueError, "exceeds configured limit"):
                transfer._extract_directory(archive, root / "output", 3, container_root=True)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
