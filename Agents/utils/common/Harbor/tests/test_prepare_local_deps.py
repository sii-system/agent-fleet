import io
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from Agents.utils.common.Harbor import prepare_local_deps


class PrepareLocalDepsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_config_preserves_environment_interface(self):
        config = prepare_local_deps.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root / "wheels"),
                "PYTHON_BIN": "python-custom",
                "CLAUDE_CODE_VERSION": "1.2.3",
                "OPENCODE_VERSION": "4.5.6",
                "PREPARE_OPENCODE_CACHE": "1",
                "NPM_CONFIG_REGISTRY": "https://npm.example.invalid/",
                "CACHE_SCHEMA": "9",
            },
            script_dir=self.root,
        )

        self.assertEqual(config.wheel_dir, self.root / "wheels")
        self.assertEqual(config.python_bin, "python-custom")
        self.assertEqual(config.claude_code_version, "1.2.3")
        self.assertEqual(config.opencode_version, "4.5.6")
        self.assertTrue(config.prepare_opencode_cache)
        self.assertEqual(config.npm_registry_url, "https://npm.example.invalid/")
        self.assertEqual(config.cache_schema, "9")

    def test_tarball_ready_rejects_corrupt_archive(self):
        valid = self.root / "valid.tar.gz"
        with tarfile.open(valid, "w:gz") as archive:
            data = b"fixture"
            info = tarfile.TarInfo("fixture.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        corrupt = self.root / "corrupt.tar.gz"
        corrupt.write_text("not an archive", encoding="utf-8")

        self.assertTrue(prepare_local_deps.tarball_ready(valid))
        self.assertFalse(prepare_local_deps.tarball_ready(corrupt))
        self.assertFalse(prepare_local_deps.tarball_ready(self.root / "missing"))

    def test_npm_tarball_urls_prefers_configured_mirror_then_origin(self):
        original = (
            "https://registry.npmjs.org/opencode-linux-x64/"
            "-/opencode-linux-x64-1.0.0.tgz"
        )

        urls = prepare_local_deps.npm_tarball_urls(
            original, "https://nexus.example.invalid/repository/npm/"
        )

        self.assertEqual(
            urls,
            [
                (
                    "https://nexus.example.invalid/repository/npm/"
                    "opencode-linux-x64/-/opencode-linux-x64-1.0.0.tgz"
                ),
                original,
            ],
        )
        self.assertEqual(
            prepare_local_deps.npm_tarball_urls(
                original, "https://registry.npmjs.org"
            ),
            [original],
        )

    def test_render_manifest_matches_existing_fields(self):
        config = prepare_local_deps.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root / "wheels"),
                "PYTHON_BIN": "python3.12",
                "CLAUDE_CODE_VERSION": "1.2.3",
                "OPENCODE_VERSION": "4.5.6",
                "PREPARE_OPENCODE_CACHE": "0",
                "CACHE_SCHEMA": "3",
            },
            script_dir=self.root,
        )

        manifest = prepare_local_deps.render_manifest(
            config, datetime(2026, 8, 19, 1, 2, 3, tzinfo=timezone.utc)
        )

        self.assertIn("generated_at=2026-08-19T01:02:03Z\n", manifest)
        self.assertIn("cache_schema=3\n", manifest)
        self.assertIn("python_bin=python3.12\n", manifest)
        self.assertIn("claude_code_version=1.2.3\n", manifest)
        self.assertIn("prepare_opencode_cache=0\n", manifest)
        self.assertNotIn("opencode-ai@4.5.6", manifest)

    def test_shell_wrapper_only_selects_and_executes_python(self):
        wrapper = Path(__file__).resolve().parents[1] / "prepare_local_deps.sh"
        content = wrapper.read_text(encoding="utf-8")

        self.assertNotIn("<<PY", content)
        self.assertNotIn("<<'PY'", content)
        self.assertIn('exec "$PYTHON_BIN" "$SCRIPT_DIR/prepare_local_deps.py"', content)
        self.assertLessEqual(len(content.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
