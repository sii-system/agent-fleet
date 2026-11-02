"""Runtime APT mirror regressions for native task source files."""

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "claude_apt_sources", Path(__file__).parents[1] / "apt_sources.py"
)
apt_sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apt_sources)

MIRRORS = {
    "HARBOR_APT_UBUNTU_MIRROR": "http://apt.invalid/ubuntu/",
    "HARBOR_APT_DEBIAN_MIRROR": "http://apt.invalid/debian/",
    "HARBOR_APT_DEBIAN_SECURITY_MIRROR": "http://apt.invalid/debian-security/",
}


class AptSourcesTest(unittest.TestCase):
    def test_rewrites_public_sources_and_preserves_options_private_and_commented_entries(self):
        text = (
            "deb [signed-by=/keys/debian.gpg] http://deb.debian.org/debian bullseye main\n"
            "deb https://security.debian.org/debian-security bullseye-security main\n"
            "deb http://us.archive.ubuntu.com/ubuntu focal main\n"
            "# deb http://deb.debian.org/debian ignored main\n"
            "deb https://user:fake-password@deb.debian.org/debian private main\n"
            "deb https://deb.debian.org/debian?token=fake private main\n"
            "deb https://packages.example.invalid/custom custom main\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sources.list"
            source.write_text(text)
            source.chmod(0o640)
            self.assertEqual(apt_sources.configure_sources(root, MIRRORS), 1)
            rewritten = source.read_text()
            self.assertIn("[signed-by=/keys/debian.gpg] http://apt.invalid/debian bullseye", rewritten)
            self.assertIn("http://apt.invalid/debian-security bullseye-security", rewritten)
            self.assertIn("http://apt.invalid/ubuntu focal", rewritten)
            self.assertEqual(rewritten.splitlines()[3:], text.splitlines()[3:])
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)
            self.assertEqual(apt_sources.configure_sources(root, MIRRORS), 0)

    def test_rewrites_deb822_uri_continuations_without_changing_signatures(self):
        text = (
            "Types: deb\nURIs: http://deb.debian.org/debian\n"
            " https://security.debian.org/debian-security\n"
            "# http://deb.debian.org/debian is documented here\n"
            "Suites: bookworm bookworm-updates\nComponents: main\n"
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
        )
        expected = text.replace("URIs: http://deb.debian.org/debian", "URIs: http://apt.invalid/debian")
        expected = expected.replace(" https://security.debian.org/debian-security", " http://apt.invalid/debian-security")
        self.assertEqual(apt_sources.rewrite_content(text, MIRRORS, deb822=True), expected)

    def test_explicit_empty_mirror_preserves_sources(self):
        text = "deb http://deb.debian.org/debian bullseye main\n"
        self.assertEqual(apt_sources.rewrite_content(text, {}, deb822=False), text)
        self.assertEqual(apt_sources.rewrite_content(text, {"HARBOR_APT_DEBIAN_MIRROR": ""}, deb822=False), text)

    def test_rejects_unsafe_mirror_before_mutation(self):
        for value in ("http://apt.invalid/repo\ndeb bad", "https://user:fake@apt.invalid/repo", "file:///tmp/repo"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "sources.list"
                source.write_text("deb http://deb.debian.org/debian bullseye main\n")
                before = source.read_bytes()
                with self.assertRaises(ValueError):
                    apt_sources.configure_sources(root, {"HARBOR_APT_DEBIAN_MIRROR": value})
                self.assertEqual(source.read_bytes(), before)

    def test_keeps_sources_bound_to_auth_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sources.list"
            source.write_text("deb http://deb.debian.org/debian bullseye main\n")
            before = source.read_bytes()
            auth = root / "auth.conf"
            auth.write_text("  machine deb.debian.org login fake password fake\n")
            self.assertEqual(apt_sources.configure_sources(root, MIRRORS), 0)
            self.assertEqual(source.read_bytes(), before)
            auth.unlink()
            target = root / "target"
            source.rename(target)
            source.symlink_to(target)
            self.assertEqual(apt_sources.configure_sources(root, MIRRORS), 0)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
