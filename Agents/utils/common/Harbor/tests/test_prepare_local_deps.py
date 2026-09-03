import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
                "PI_VERSION": "0.81.1",
                "PREPARE_PI_CACHE": "1",
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
        self.assertEqual(config.pi_version, "0.81.1")
        self.assertTrue(config.prepare_pi_cache)
        self.assertEqual(
            config.pi_runtime_tarball, self.root / "wheels" / "pi-runtime-0.81.1.tar.gz"
        )
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
        with mock.patch.object(
            prepare_local_deps.tarfile, "open", side_effect=EOFError
        ):
            self.assertFalse(prepare_local_deps.tarball_ready(valid))

    @staticmethod
    def _write_npm_tarball(path: Path, version: str) -> None:
        payload = json.dumps({"name": "fixture", "version": version}).encode()
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def test_npm_tarball_version_reads_embedded_package_version(self):
        archive = self.root / "package.tgz"
        self._write_npm_tarball(archive, "1.2.3")

        self.assertEqual(
            prepare_local_deps.npm_tarball_version(archive), "1.2.3"
        )
        self.assertIsNone(
            prepare_local_deps.npm_tarball_version(self.root / "missing.tgz")
        )
        self.assertTrue(
            prepare_local_deps.npm_version_matches_selector("1.2.3", "latest")
        )
        self.assertFalse(
            prepare_local_deps.npm_version_matches_selector("9.9.9", "1.2.3")
        )

    def test_npm_pack_uses_exact_result_instead_of_newest_cached_archive(self):
        wheel_dir = self.root / "wheels"
        wheel_dir.mkdir()
        target = wheel_dir / "claude-code-1.2.3.tgz"
        stale = wheel_dir / "anthropic-ai-claude-code-9.9.9.tgz"
        self._write_npm_tarball(target, "9.9.9")
        self._write_npm_tarball(stale, "9.9.9")
        config = prepare_local_deps.Config.from_environment(
            {
                "WHEEL_DIR": str(wheel_dir),
                "CLAUDE_CODE_VERSION": "1.2.3",
            },
            script_dir=self.root,
        )

        def npm_pack(*args, **kwargs):
            package_name = "anthropic-ai-claude-code-1.2.3.tgz"
            self._write_npm_tarball(
                Path(kwargs["cwd"]) / package_name, "1.2.3"
            )
            return SimpleNamespace(stdout=f"{package_name}\n")

        with (
            mock.patch.object(prepare_local_deps.shutil, "which", return_value="npm"),
            mock.patch.object(
                prepare_local_deps.subprocess, "run", side_effect=npm_pack
            ),
        ):
            prepare_local_deps.DependencyPreparer(config)._pack_npm_to_cache(
                config.claude_code_npm_spec,
                config.claude_code_tgz_basename,
                "https://registry.example.invalid/metadata",
                config.claude_code_version,
                "anthropic-ai-claude-code-*.tgz",
            )

        self.assertEqual(
            prepare_local_deps.npm_tarball_version(target), "1.2.3"
        )
        self.assertFalse(stale.exists())

    def test_atomic_download_tries_origin_after_invalid_mirror_archive(self):
        target = self.root / "package.tgz"
        mirror = "https://mirror.example.invalid/package.tgz"
        origin = "https://registry.example.invalid/package.tgz"
        calls = []

        def download(url, destination, *, timeout=None):
            calls.append(url)
            if url == mirror:
                destination.write_text("invalid", encoding="utf-8")
            else:
                self._write_npm_tarball(destination, "1.2.3")

        with mock.patch.object(
            prepare_local_deps, "_download", side_effect=download
        ):
            selected = prepare_local_deps._download_atomic(
                [mirror, origin],
                target,
                prefix="fixture-",
                suffix=".tgz",
                validate=lambda path: (
                    prepare_local_deps.npm_tarball_version(path) == "1.2.3"
                ),
                label="fixture",
            )

        self.assertEqual(selected, origin)
        self.assertEqual(calls, [mirror, origin])
        self.assertEqual(
            prepare_local_deps.npm_tarball_version(target), "1.2.3"
        )

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
                "PI_VERSION": "0.81.1",
                "PREPARE_PI_CACHE": "1",
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
        self.assertIn("prepare_pi_cache=1\n", manifest)
        self.assertIn("pi_runtime_version=0.81.1\n", manifest)
        self.assertIn("@earendil-works/pi-coding-agent@0.81.1", manifest)

    def test_pi_portable_node_runtime_recompresses_node_tarball(self):
        wheel_dir = self.root / "wheels"
        wheel_dir.mkdir()
        source = wheel_dir / "node-runtime.tar.xz"
        with tarfile.open(source, "w:xz") as archive:
            payload = b"fixture-node"
            info = tarfile.TarInfo("node-v22/bin/node")
            info.mode = 0o755
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config = prepare_local_deps.Config.from_environment(
            {
                "WHEEL_DIR": str(wheel_dir),
                "PREPARE_PI_CACHE": "1",
            },
            script_dir=self.root,
        )

        prepare_local_deps.DependencyPreparer(config)._prepare_pi_node_runtime_tarball()

        self.assertTrue(prepare_local_deps.tarball_ready(config.pi_node_runtime_tarball))
        with tarfile.open(config.pi_node_runtime_tarball, "r:gz") as archive:
            member = archive.extractfile("node-v22/bin/node")
            self.assertIsNotNone(member)
            assert member is not None
            self.assertEqual(member.read(), b"fixture-node")

    def test_shell_wrapper_only_selects_and_executes_python(self):
        wrapper = Path(__file__).resolve().parents[1] / "prepare_local_deps.sh"
        content = wrapper.read_text(encoding="utf-8")

        self.assertNotIn("<<PY", content)
        self.assertNotIn("<<'PY'", content)
        self.assertIn('exec "$PYTHON_BIN" "$SCRIPT_DIR/prepare_local_deps.py"', content)
        self.assertLessEqual(len(content.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
