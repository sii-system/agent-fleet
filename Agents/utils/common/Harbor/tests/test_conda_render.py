"""Conda routing preserves task inputs and named-channel resolution settings."""

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from task_image_manager.build_engine.dockerfile_renderer.renderer import (
    render_build_dockerfile,
)
from task_image_manager.build_engine.dockerfile_renderer.strategies.conda import (
    materialize_conda_config,
    rewrite_conda_source_urls,
)
from task_image_manager.build_engine.dockerfile_renderer.strategies.packages import (
    materialize_package_source_context,
    package_source_build_args,
    package_source_hosts,
)


class CondaRenderTest(unittest.TestCase):
    defaults = "http://gateway.internal/v1/cache/conda-defaults"
    channels = "http://gateway.internal/v1/cache/conda-channels"

    def test_build_args_validate_endpoints_and_include_direct_hosts(self):
        args = package_source_build_args(Namespace(
            conda_defaults_url=self.defaults, conda_channels_url=self.channels
        ), "host")
        self.assertIn("gateway.internal", package_source_hosts(args))
        self.assertEqual(args["CONDA_DEFAULTS_URL"], self.defaults)
        self.assertEqual(args["CONDA_CHANNELS_URL"], self.channels)
        for value in ("https://user:secret@host/main", "https://host/channels?token=x",
                      "http://127.0.0.1:8080/channels"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                package_source_build_args(Namespace(conda_defaults_url=value), "default")
        empty = package_source_build_args(Namespace(), "host")
        self.assertNotIn("CONDA_DEFAULTS_URL", empty)
        self.assertNotIn("CONDA_CHANNELS_URL", empty)

    def test_config_maps_named_channels_without_changing_solver_or_other_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets = materialize_conda_config(root, self.defaults, self.channels)
            config = json.loads(next(iter(secrets.values())).read_text())
            self.assertEqual(config, {
                "default_channels": [self.defaults + "/main", self.defaults + "/r"],
                "custom_channels": {"conda-forge": self.channels},
            })
            self.assertEqual(secrets, materialize_conda_config(root, self.defaults, self.channels))
            changed = materialize_conda_config(root / "changed", self.defaults, self.channels + "-new")
            self.assertNotEqual(set(secrets), set(changed))
            self.assertEqual({}, materialize_conda_config(root / "disabled"))
            self.assertFalse((root / "disabled").exists())

    def test_reviewed_urls_keep_subdir_package_and_channel_order(self):
        source = (
            "micromamba create --strict-channel-priority --override-channels "
            "-c https://conda.anaconda.org/conda-forge "
            "-c https://repo.anaconda.com/pkgs/main python=3.10\n"
            "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r/noarch/repodata.json\n"
            "https://repo.continuum.io/pkgs/main/linux-64/example-1.0-0.conda\n"
        )
        updated = rewrite_conda_source_urls(source, self.defaults, self.channels)
        self.assertIn("-c " + self.channels + "/conda-forge -c " + self.defaults + "/main python=3.10", updated)
        self.assertIn(self.defaults + "/r/noarch/repodata.json", updated)
        self.assertIn(self.defaults + "/main/linux-64/example-1.0-0.conda", updated)
        self.assertIn("--strict-channel-priority --override-channels", updated)

    def test_auth_queries_unsupported_channels_and_lookalikes_keep_authored_transport(self):
        values = [
            "https://user:fake@repo.anaconda.com/pkgs/main",
            "https://repo.anaconda.com/pkgs/main?token=fake",
            "https://repo.anaconda.com/pkgs/main#fragment",
            "https://repo.anaconda.com/pkgs/main?",
            "https://repo.anaconda.com:443/pkgs/main",
            "https://repo.anaconda.com.evil.example/pkgs/main",
            "https://conda.anaconda.org/t/fake/conda-forge",
            "https://conda.anaconda.org/pytorch",
            "https://repo.anaconda.com/pkgs/free",
            "https://repo.anaconda.com/pkgs/main/osx-arm64/repodata.json",
            "https://repo.anaconda.com/pkgs/main/../r",
            "https://repo.anaconda.com/miniconda/Miniconda3.sh",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(value, rewrite_conda_source_urls(value, self.defaults, self.channels))

    def test_run_forms_and_multistage_args_preserve_copy_heredoc(self):
        source = (
            "FROM base AS build\n"
            "ENV AUTHORED_CHANNEL=https://repo.anaconda.com/pkgs/main\n"
            "RUN [\"conda\", \"install\", \"-c\", \"https://conda.anaconda.org/conda-forge\", \"six=1.16\"]\n"
            "RUN <<EOF\n"
            "mamba install -c https://repo.anaconda.com/pkgs/main six\n"
            "EOF\n"
            "COPY <<YAML /tmp/notes\n"
            "https://repo.anaconda.com/pkgs/main\n"
            "YAML\n"
            "FROM build\n"
            "RUN conda install conda-forge::six=1.16\n"
            "CMD [\"conda\", \"install\", \"-c\", \"https://conda.anaconda.org/conda-forge\", \"six\"]\n"
        )
        rendered = render_build_dockerfile(source, dockerhub_mirror_prefix="", package_build_args={
            "CONDA_DEFAULTS_URL": self.defaults, "CONDA_CHANNELS_URL": self.channels
        })
        self.assertIn('"' + self.channels + '/conda-forge"', rendered)
        self.assertIn("mamba install -c " + self.defaults + "/main six", rendered)
        self.assertIn("COPY <<YAML /tmp/notes\nhttps://repo.anaconda.com/pkgs/main\nYAML", rendered)
        self.assertIn("conda-forge::six=1.16", rendered)
        self.assertIn("ENV AUTHORED_CHANNEL=https://repo.anaconda.com/pkgs/main", rendered)
        self.assertIn('CMD ["conda", "install", "-c", "https://conda.anaconda.org/conda-forge", "six"]', rendered)
        self.assertEqual(rendered.count("ARG CONDA_DEFAULTS_URL"), 2)
        self.assertNotIn("ENV CONDA", rendered)

    def test_temporary_context_handles_environment_files_and_preserves_originals_and_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            files = {
                "environment.yml": "channels:\n  - https://conda.anaconda.org/conda-forge\n  - defaults\ndependencies:\n  - python=3.10\n  - pip:\n    - --index-url https://download.pytorch.org/whl/cpu\n",
                ".condarc": "default_channels:\n  - https://repo.anaconda.com/pkgs/main\nchannel_priority: strict\n",
                "setup.sh": "#!/bin/sh\nconda install -c https://conda.anaconda.org/conda-forge six\n",
                "notes.txt": "https://conda.anaconda.org/conda-forge\n",
                "Dockerfile": "FROM base\nENV CHANNEL=https://conda.anaconda.org/conda-forge\n",
            }
            for name, content in files.items():
                (source / name).write_text(content)
            (source / "setup.sh").chmod(0o755)
            (source / "linked.yml").symlink_to("environment.yml")
            context, rewritten = materialize_package_source_context(
                source, root / "rendered", conda_defaults_url=self.defaults, conda_channels_url=self.channels,
                pytorch_index_url="http://gateway.internal/pytorch",
            )
            self.assertEqual(set(rewritten), {"environment.yml", ".condarc", "setup.sh"})
            for name, content in files.items():
                self.assertEqual((source / name).read_text(), content)
            self.assertIn(self.channels + "/conda-forge", (context / "environment.yml").read_text())
            self.assertIn("python=3.10", (context / "environment.yml").read_text())
            self.assertIn("https://download.pytorch.org/whl/cpu", (context / "environment.yml").read_text())
            self.assertEqual((context / "notes.txt").read_text(), files["notes.txt"])
            self.assertEqual((context / "Dockerfile").read_text(), files["Dockerfile"])
            self.assertTrue((context / "linked.yml").is_symlink())
            self.assertEqual((context / "setup.sh").stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
