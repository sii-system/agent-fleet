from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import prepare_dsh_sdk_minimal_cli_runtime as cli_runtime  # noqa: E402
import prepare_dsh_sdk_minimal_runtime as sdk_runtime  # noqa: E402


class PrepareDshCliRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_and_cache_require_the_pinned_cli_version(self) -> None:
        config = cli_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_CLI_VERSION": "0.1.2-alpha.2",
                "NPM_CONFIG_REGISTRY": "https://npm.example.test",
            }
        )
        self.assertEqual(
            config.runtime_tarball,
            self.root / "dsh-sdk-minimal-cli-runtime-0.1.2-alpha.2.tar.gz",
        )
        self.assertEqual(config.npm_registry_url, "https://npm.example.test")
        self.assertEqual(config.npm_cache_dir, self.root / "dsh-sdk-minimal-npm-cache")
        with tarfile.open(config.runtime_tarball, "w:gz") as archive:
            payload = b"runtime"
            info = tarfile.TarInfo("bin/dsh")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config.version_file.write_text("0.1.2-alpha.1\n", encoding="utf-8")
        self.assertFalse(cli_runtime.runtime_ready(config))
        config.version_file.write_text("0.1.2-alpha.2\n", encoding="utf-8")
        self.assertTrue(cli_runtime.runtime_ready(config))

    def test_prepare_converts_node_archive_for_minimal_images(self) -> None:
        config = cli_runtime.Config.from_environment(
            {"WHEEL_DIR": str(self.root), "DSH_CLI_VERSION": "0.1.2-alpha.2"}
        )
        with tarfile.open(config.node_runtime_tarball, "w:xz") as archive:
            for name, payload in (
                ("node-v22/bin/node", b"#!/bin/sh\necho 22\n"),
                ("node-v22/bin/npm", b"#!/bin/sh\nexit 0\n"),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        with tarfile.open(config.runtime_tarball, "w:gz") as archive:
            payload = b"runtime"
            info = tarfile.TarInfo("bin/dsh")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config.version_file.write_text(config.version, encoding="utf-8")

        cli_runtime.prepare(config)

        self.assertTrue(cli_runtime.tarball_ready(config.portable_node_runtime_tarball))


class PrepareDshSdkRuntimeTests(unittest.TestCase):
    @staticmethod
    def _write_tarball(path: Path) -> None:
        payload = path.with_suffix(".payload")
        payload.write_text("runtime", encoding="utf-8")
        with tarfile.open(path, "w:gz") as archive:
            archive.add(payload, arcname="runtime")

    def test_cache_identity_pins_source_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            config = sdk_runtime.Config.from_environment({"WHEEL_DIR": temporary_name})
            self._write_tarball(config.runtime_tarball)
            self._write_tarball(config.python_runtime_tarball)
            config.version_file.write_text(config.source_version, encoding="utf-8")
            self.assertFalse(sdk_runtime.runtime_ready(config))
            config.version_file.write_text(config.runtime_version, encoding="utf-8")
            self.assertTrue(sdk_runtime.runtime_ready(config))
            self.assertEqual(config.source_ref, "dsh-v0.1.2-alpha.2")
            self.assertEqual(
                config.source_sha, "0a53fb55bea101816fa226bb964ae2bed71c343b"
            )
            self.assertIn("pydantic==2.13.4", config.runtime_version)

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(sdk_runtime.main(["--print-runtime-version"]), 0)
        self.assertEqual(
            output.getvalue().strip(),
            sdk_runtime.Config.from_environment().runtime_version,
        )

    def test_local_sdk_source_requires_exact_clean_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            sdk_dir = root / "python" / "sdk"
            sdk_dir.mkdir(parents=True)
            pyproject = sdk_dir / "pyproject.toml"
            pyproject.write_text(
                '[project]\nname = "deepseek-harness-sdk"\n', encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"], check=True
            )
            subprocess.run(
                ["git", "-C", str(root), "add", "python/sdk/pyproject.toml"], check=True
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "fixture"], check=True
            )
            sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            config = sdk_runtime.Config.from_environment(
                {
                    "WHEEL_DIR": str(root / "wheels"),
                    "DSH_SDK_MINIMAL_SOURCE_DIR": str(root),
                    "DSH_SDK_MINIMAL_SOURCE_SHA": sha,
                }
            )
            self.assertEqual(sdk_runtime.verified_sdk_source(config), sdk_dir)
            pyproject.write_text("local edit\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                sdk_runtime.verified_sdk_source(config)

    def test_python_runtime_must_be_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            executable = root / "bin" / "python3.12"
            executable.parent.mkdir()
            executable.touch()
            patches = (
                mock.patch.object(sdk_runtime.sys, "executable", str(executable)),
                mock.patch.object(sdk_runtime.sys, "version_info", (3, 12, 0)),
            )
            with (
                patches[0],
                patches[1],
                self.assertRaisesRegex(RuntimeError, "python-build-standalone"),
            ):
                sdk_runtime.managed_python_root()

            (root / "BUILD").touch()
            with (
                mock.patch.object(sdk_runtime.sys, "executable", str(executable)),
                mock.patch.object(sdk_runtime.sys, "version_info", (3, 12, 0)),
            ):
                self.assertEqual(sdk_runtime.managed_python_root(), root)


if __name__ == "__main__":
    unittest.main()
