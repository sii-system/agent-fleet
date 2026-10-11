"""Build boundary regression tests without Docker or Registry access."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from task_image_manager.build_engine import build_image, image_build


class ImageBuildTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.context = self.root / "source"
        self.context.mkdir()
        self.dockerfile = self.context / "Dockerfile"
        self.original = "FROM ubuntu:24.04\nRUN echo hello\n"
        self.dockerfile.write_text(self.original)
        self.work = self.root / "work"
        self.log = self.root / "build.log"
        self.config = {"entrypoint": None, "cmd": ["bash"], "exposed_ports": []}
        self.stack.enter_context(
            patch.object(
                image_build,
                "resolve_base_image_contexts",
                return_value=(
                    {"ubuntu:24.04": "base"},
                    {"base": "docker-image://example/base@sha256:abc"},
                ),
            )
        )
        self.frontend = self.stack.enter_context(
            patch.object(
                image_build,
                "prepare_frontend",
                return_value={"FRONTEND_ARG": "enabled"},
            )
        )
        self.build = self.stack.enter_context(
            patch.object(
                image_build,
                "run_build",
                side_effect=self.execute,
            )
        )
        self.metadata = self.stack.enter_context(
            patch.object(
                image_build,
                "oci_archive_image_config",
                return_value=self.config,
            )
        )
        self.options = {
            "context_dir": self.context,
            "dockerfile": self.dockerfile,
            "temporary_root": self.work,
            "log_path": self.log,
            "platform": "linux/amd64",
            "timeout_sec": 42,
            "build_args": {"TASK_ARG": "task", "HTTP_PROXY": "http://proxy.example"},
            "package_build_args": {},
            "target": "final",
            "no_cache": True,
            "build_network": "host",
            "github_mirror_url": "https://gateway.example/v1/git/github/",
            "download_source_url": "https://gateway.example/v1/cache",
        }

    def execute(self, **kwargs):
        # Build inputs must remain alive until the executor finishes.
        self.assertTrue(kwargs["dockerfile"].is_file())
        self.assertTrue(kwargs["environment_dir"].is_dir())
        self.assertTrue(kwargs["secret_files"])
        for path in kwargs["secret_files"].values():
            self.assertTrue(path.is_file())
        kwargs["archive_path"].write_bytes(b"test archive")
        kwargs["log_path"].write_text("build output")

    def test_build_inputs_and_archive_lifetime(self):
        with build_image(**self.options) as built:
            self.assertEqual(built.archive_path.read_bytes(), b"test archive")
            self.assertEqual(built.config, self.config)
            self.metadata.assert_called_once_with(built.archive_path)
            inputs = self.build.call_args.kwargs
            self.assertEqual(
                inputs["build_args"],
                {
                    "TASK_ARG": "task",
                    "HTTP_PROXY": "http://proxy.example",
                    "FRONTEND_ARG": "enabled",
                },
            )
            self.assertEqual(inputs["target"], "final")
            self.assertEqual(inputs["timeout_sec"], 42)
            self.assertEqual(inputs["platform"], "linux/amd64")
            self.assertEqual(inputs["build_network"], "host")
            self.assertTrue(inputs["no_cache"])
            self.assertEqual(
                inputs["build_contexts"]["base"],
                "docker-image://example/base@sha256:abc",
            )
            self.assertIn("FROM base", inputs["dockerfile"].read_text())
            self.assertTrue(any("git" in key for key in inputs["secret_files"]))
            self.assertEqual(self.dockerfile.read_text(), self.original)
        self.assertFalse(built.archive_path.exists())
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(self.log.read_text(), "build output")
        self.assertNotIn("FRONTEND_ARG", self.options["build_args"])

    def test_cleanup_on_preparation_build_metadata_or_consumer_failure(self):
        for stage in ("frontend", "build", "metadata", "consumer"):
            with self.subTest(stage=stage):
                self.frontend.side_effect = None
                self.build.side_effect = self.execute
                self.metadata.side_effect = None
                failure = RuntimeError(stage)
                if stage != "consumer":
                    getattr(self, stage).side_effect = failure
                with (
                    self.assertRaisesRegex(RuntimeError, stage),
                    build_image(**self.options),
                ):
                    if stage == "consumer":
                        raise failure
                    self.fail("failed build must not expose an artifact")
                self.assertEqual(list(self.work.iterdir()), [])
                self.assertEqual(self.dockerfile.read_text(), self.original)

    def test_conda_uses_effective_build_args_for_context_rendering_and_temporary_config(self):
        self.original = "FROM ubuntu:24.04\nRUN conda install -c https://repo.anaconda.com/pkgs/main six\n"
        self.dockerfile.write_text(self.original)
        env_source = "channels:\n  - https://conda.anaconda.org/conda-forge\n"
        (self.context / "env.yml").write_text(env_source)
        self.options["package_build_args"] = {
            "CONDA_DEFAULTS_URL": "http://gateway/defaults",
            "CONDA_CHANNELS_URL": "http://gateway/channels",
        }
        self.options["build_args"].update({
            "CONDA_DEFAULTS_URL": "http://explicit/defaults",
            "CONDA_CHANNELS_URL": "http://explicit/channels",
        })
        with build_image(**self.options):
            inputs = self.build.call_args.kwargs
            self.assertIn("http://explicit/defaults/main", inputs["dockerfile"].read_text())
            self.assertIn("http://explicit/channels/conda-forge", (inputs["environment_dir"] / "env.yml").read_text())
            config = next(path for key, path in inputs["secret_files"].items() if key.startswith("opensandbox-conda-config-"))
            self.assertIn("http://explicit/defaults/main", config.read_text())
            self.assertNotIn("http://gateway", config.read_text())
            self.assertEqual((self.context / "env.yml").read_text(), env_source)
            self.assertEqual(self.dockerfile.read_text(), self.original)
        self.assertFalse(config.exists())

    def test_explicit_empty_conda_roots_disable_config_and_rewrites(self):
        self.original = "FROM ubuntu:24.04\nRUN conda install -c https://repo.anaconda.com/pkgs/main six\n"
        self.dockerfile.write_text(self.original)
        self.options["package_build_args"] = {"CONDA_DEFAULTS_URL": "http://gateway/defaults"}
        self.options["build_args"]["CONDA_DEFAULTS_URL"] = ""
        with build_image(**self.options):
            inputs = self.build.call_args.kwargs
            self.assertIn("https://repo.anaconda.com/pkgs/main", inputs["dockerfile"].read_text())
            self.assertFalse(any(key.startswith("opensandbox-conda-config-") for key in inputs["secret_files"]))


if __name__ == "__main__":
    unittest.main()
