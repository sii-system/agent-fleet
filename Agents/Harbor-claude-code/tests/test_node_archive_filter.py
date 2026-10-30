"""Exercise the exact installer heredoc, including the CPython gh-107845 bug."""

import ast
import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).parents[1] / "sitecustomize.py"
EXTRACT = next(
    ast.literal_eval(node.value)
    for node in ast.parse(SOURCE.read_text()).body
    if isinstance(node, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "_NODE_ARCHIVE_EXTRACT" for t in node.targets)
)
NATIVE_FILTER = tarfile.data_filter


def old_data_filter(member, path):
    if member.issym():
        if os.path.isabs(member.linkname):
            raise tarfile.AbsoluteLinkError(member)
        root = os.path.realpath(path)
        target = os.path.realpath(os.path.join(root, member.linkname))
        if os.path.commonpath([root, target]) != root:
            raise tarfile.LinkOutsideDestinationError(member, target)
        return tarfile.tar_filter(member, path).replace(
            mode=None, uid=None, gid=None, uname=None, gname=None
        )
    return NATIVE_FILTER(member, path)


def member(name, kind=tarfile.REGTYPE, target="", mode=0o777):
    item = tarfile.TarInfo(name)
    item.type, item.linkname, item.mode = kind, target, mode
    item.uid, item.gid, item.uname, item.gname = 123, 456, "owner", "group"
    item.size = 7 if item.isfile() else 0
    return item


class NodeArchiveFilterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "extract"
        self.destination.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "sentinel").write_text("unchanged")

    def run_archive(self, entries, legacy=False, unfiltered=False):
        archive_path = self.root / "input.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for item in entries:
                archive.addfile(item, io.BytesIO(b"content") if item.isfile() else None)
        namespace = {}
        selected = old_data_filter if legacy else NATIVE_FILTER
        with mock.patch.object(tarfile, "data_filter", selected), mock.patch.object(
            sys, "argv", ["-", str(archive_path), str(self.destination)]
        ):
            if unfiltered:
                del tarfile.data_filter
                with self.assertRaises(SystemExit) as stopped:
                    exec(EXTRACT, namespace)  # noqa: S102 - Test the repository-owned installer heredoc.
                self.assertEqual(stopped.exception.code, 0)
            else:
                exec(EXTRACT, namespace)  # noqa: S102 - Test the repository-owned installer heredoc.
        self.assertIs(tarfile.data_filter, NATIVE_FILTER)
        return namespace

    def assert_rejected(self, entries, legacy=False):
        with self.assertRaises(tarfile.FilterError):
            self.run_archive(entries, legacy=legacy)
        self.assertEqual((self.outside / "sentinel").read_text(), "unchanged")
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "sentinel"])

    def test_real_node_link_layout_and_metadata(self):
        entries = [member("node", tarfile.DIRTYPE), member("node/bin", tarfile.DIRTYPE)]
        for name in ("npm", "npx", "corepack"):
            entries += [member("node/lib/" + name),
                        member("node/bin/" + name, tarfile.SYMTYPE, "../lib/" + name)]
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                ns = self.run_archive(entries, legacy)
                for name in ("npm", "npx", "corepack"):
                    self.assertEqual((self.destination / "node/bin" / name).read_bytes(), b"content")
                with mock.patch.object(tarfile, "data_filter", old_data_filter if legacy else NATIVE_FILTER):
                    checked = ns["node_filter"](entries[-1], str(self.destination))
                self.assertIsNone(checked.mode)
                self.assertIsNone(checked.uid)
                self.assertIsNone(checked.gid)
                self.assertIsNone(checked.uname)
                self.assertIsNone(checked.gname)

    def test_legacy_probe_detects_wrong_root(self):
        self.assertTrue(self.run_archive([], legacy=True)["legacy_symlinks"])

    def test_absolute_member_path_rejected(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member(str(self.outside / "sentinel"))], legacy)

    def test_parent_member_path_rejected(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member("../outside/sentinel")], legacy)

    def test_absolute_symlink_rejected(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member("node/link", tarfile.SYMTYPE, str(self.outside))], legacy)

    def test_parent_symlink_rejected(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member("node/bin/link", tarfile.SYMTYPE, "../../../outside")], legacy)

    def test_realpath_parent_symlink_escape_rejected(self):
        (self.destination / "redirect").symlink_to(self.outside, target_is_directory=True)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member("node/link", tarfile.SYMTYPE, "../redirect/sentinel")], legacy)

    def test_regular_file_through_symlink_rejected(self):
        (self.destination / "redirect").symlink_to(self.outside, target_is_directory=True)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([member("redirect/sentinel")], legacy)

    def test_link_cannot_replace_extraction_root(self):
        for name in (".", "node/..", ""):
            with self.subTest(name=name):
                self.assert_rejected([member(name, tarfile.SYMTYPE, "node")], legacy=True)

    def test_devices_and_fifo_rejected(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            for legacy in (False, True):
                with self.subTest(kind=kind, legacy=legacy):
                    self.assert_rejected([member("special", kind)], legacy)

    def test_setuid_setgid_and_writable_mode_removed(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.run_archive([member("executable", mode=0o6777)], legacy)
                self.assertEqual(stat.S_IMODE((self.destination / "executable").stat().st_mode), 0o755)

    def test_existing_internal_hardlink_works(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.run_archive([member("target"), member("hard", tarfile.LNKTYPE, "target")], legacy)
                self.assertEqual((self.destination / "hard").read_bytes(), b"content")

    def test_hardlink_escape_rejected(self):
        for target in ("../outside/sentinel", str(self.outside / "sentinel")):
            for legacy in (False, True):
                with self.subTest(target=target, legacy=legacy):
                    self.assert_rejected([member("hard", tarfile.LNKTYPE, target)], legacy)

    def test_legacy_hardlink_to_symlink_and_forward_fallback_rejected(self):
        self.run_archive([member("target"), member("sym", tarfile.SYMTYPE, "target")])
        for target in ("sym", "missing"):
            with self.subTest(target=target):
                self.assert_rejected([member("hard", tarfile.LNKTYPE, target)], legacy=True)

    def test_normalizes_symlink_target(self):
        self.run_archive([member("node/target"),
                          member("node/link", tarfile.SYMTYPE, "sub/../target")], legacy=True)
        self.assertEqual(os.readlink(self.destination / "node/link"), "target")

    def test_missing_data_filter_extracts_private_node(self):
        self.run_archive([member("node/bin", tarfile.DIRTYPE),
                          member("node/lib/npm-cli.js", mode=0o6777),
                          member("node/bin/npm", tarfile.SYMTYPE, "../lib/npm-cli.js")],
                         unfiltered=True)
        self.assertEqual((self.destination / "node/bin/npm").read_bytes(), b"content")
        self.assertEqual(stat.S_IMODE((self.destination / "node/lib/npm-cli.js").stat().st_mode), 0o755)

    def test_missing_data_filter_rejects_unsafe_members(self):
        bad = [member(str(self.outside / "sentinel")), member("../outside/sentinel"),
               member("node/link", tarfile.SYMTYPE, "../../outside"),
               member("node/link", tarfile.SYMTYPE, str(self.outside)),
               member(".", tarfile.SYMTYPE, "node"),
               member("hard", tarfile.LNKTYPE, "missing"),
               member("hard", tarfile.LNKTYPE, "../outside/sentinel"),
               member("device", tarfile.CHRTYPE), member("fifo", tarfile.FIFOTYPE)]
        for item in bad:
            with self.subTest(name=item.name, kind=item.type):
                with self.assertRaises(tarfile.FilterError):
                    self.run_archive([item], unfiltered=True)
                self.assertEqual((self.outside / "sentinel").read_text(), "unchanged")

    def test_missing_data_filter_checks_symlink_pivots_sequentially(self):
        (self.destination / "redirect").symlink_to(self.outside, target_is_directory=True)
        for item in (member("redirect/sentinel"),
                     member("node/link", tarfile.SYMTYPE, "../redirect/sentinel"),
                     member("hard", tarfile.LNKTYPE, "redirect/sentinel")):
            with self.subTest(name=item.name):
                with self.assertRaises(tarfile.FilterError):
                    self.run_archive([item], unfiltered=True)
                self.assertEqual((self.outside / "sentinel").read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
