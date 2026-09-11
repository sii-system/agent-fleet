import hashlib
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import script_utils


class ScriptUtilsTest(unittest.TestCase):
    def test_owned_shell_scripts_do_not_embed_python_heredocs(self):
        root = Path(__file__).resolve().parents[2]
        offenders = []
        pattern = re.compile(r"(?:python(?:3)?|\$PYTHON_BIN)[^\n]*<<")
        for relative_path in ("scripts/dind-run.sh", "scripts/prerequisites.sh"):
            path = root / relative_path
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(root)))

        self.assertEqual(offenders, [])

    def test_url_hostname(self):
        self.assertEqual(
            script_utils.url_hostname("https://gateway.example.invalid:8443/v1"),
            "gateway.example.invalid",
        )
        self.assertEqual(script_utils.url_hostname(""), "")

    def test_extract_toolchain_preserves_executable_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "go.zip"
            with zipfile.ZipFile(source, "w") as archive:
                entry = zipfile.ZipInfo("toolchain/bin/go")
                entry.external_attr = 0o100755 << 16
                archive.writestr(entry, "fixture")
            script_utils.extract_toolchain(source, root / "output")
            binary = root / "output/toolchain/bin/go"
            self.assertEqual(binary.read_text(), "fixture")
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("../escape", "fixture")
            with self.assertRaises(ValueError):
                script_utils.extract_toolchain(source, root / "output")

    def test_verify_sha256(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "archive"
            checksum = root / "archive.sha256"
            source.write_bytes(b"fixture")
            checksum.write_text(
                f"{hashlib.sha256(b'fixture').hexdigest()}  archive\n",
                encoding="utf-8",
            )

            self.assertTrue(script_utils.verify_sha256(source, checksum))
            source.write_bytes(b"changed")
            self.assertFalse(script_utils.verify_sha256(source, checksum))


if __name__ == "__main__":
    unittest.main()
