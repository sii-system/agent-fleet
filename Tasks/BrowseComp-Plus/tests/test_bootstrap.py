from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bootstrap  # noqa: E402


class BootstrapTest(unittest.TestCase):
    def test_runtime_bootstrap_is_idempotent_and_local_judge_is_additive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root)
            commands: list[list[str]] = []

            def fake_execute(command: list[str], env=None) -> None:
                commands.append(command)
                if command[1:3] == ["venv", "--python"]:
                    python = Path(command[-1]) / "bin" / "python"
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.write_text("fixture\n", encoding="utf-8")

            with (
                patch.object(bootstrap.shutil, "which", return_value="/managed/uv"),
                patch.object(bootstrap, "has_nvidia_gpu", return_value=True),
                patch.object(bootstrap, "execute", side_effect=fake_execute),
            ):
                python = bootstrap.ensure_runtime(cache, local_judge=False)
                self.assertEqual(python, cache / "runtime" / "venv" / "bin" / "python")
                first_count = len(commands)
                bootstrap.ensure_runtime(cache, local_judge=False)
                self.assertEqual(len(commands), first_count)

                bootstrap.ensure_runtime(cache, local_judge=True)
                local_count = len(commands)
                self.assertTrue(
                    any("requirements-local-judge.lock" in " ".join(command) for command in commands)
                )
                bootstrap.ensure_runtime(cache, local_judge=True)
                bootstrap.ensure_runtime(cache, local_judge=False)
                self.assertEqual(len(commands), local_count)

    def test_local_judge_reports_gpu_requirement_before_installing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(bootstrap.shutil, "which", return_value="/managed/uv"),
            patch.object(bootstrap, "has_nvidia_gpu", return_value=False),
            self.assertRaisesRegex(RuntimeError, "requires a visible NVIDIA GPU"),
        ):
            bootstrap.ensure_runtime(Path(root), local_judge=True)

    def test_remote_embedding_runtime_skips_torch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root)
            commands: list[list[str]] = []

            def fake_execute(command: list[str], env=None) -> None:
                commands.append(command)
                if command[1:3] == ["venv", "--python"]:
                    python = Path(command[-1]) / "bin" / "python"
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.write_text("fixture\n", encoding="utf-8")

            with (
                patch.object(bootstrap.shutil, "which", return_value="/managed/uv"),
                patch.object(bootstrap, "has_nvidia_gpu", return_value=False),
                patch.object(bootstrap, "execute", side_effect=fake_execute),
            ):
                bootstrap.ensure_runtime(cache, local_judge=False, with_torch=False)

            self.assertFalse(any("torch==2.7.1" in command for command in commands))

    def test_remote_embedding_tokenizer_uses_bootstrap_network_environment(self) -> None:
        commands: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_execute(command: list[str], env=None) -> None:
            commands.append((command, env))

        with tempfile.TemporaryDirectory() as root:
            cache = Path(root)
            with patch.object(bootstrap, "execute", side_effect=fake_execute):
                bootstrap.ensure_remote_tokenizer(
                    cache,
                    Path("/fixture/python"),
                    "Qwen/Qwen3-Embedding-0.6B",
                    "fixture-revision",
                    {"HF_HOME": "/fixture/hf", "HTTPS_PROXY": "http://proxy"},
                )
                bootstrap.ensure_remote_tokenizer(
                    cache,
                    Path("/fixture/python"),
                    "Qwen/Qwen3-Embedding-0.6B",
                    "fixture-revision",
                    {"HF_HOME": "/fixture/hf", "HTTPS_PROXY": "http://proxy"},
                )

            self.assertEqual(commands[0][0][0], "/fixture/python")
            self.assertEqual(
                commands[0][0][-2:], ["Qwen/Qwen3-Embedding-0.6B", "fixture-revision"]
            )
            self.assertEqual(
                commands[0][1],
                {"HF_HOME": "/fixture/hf", "HTTPS_PROXY": "http://proxy"},
            )
            self.assertEqual(len(commands), 1)

    def test_huggingface_auto_falls_back_to_direct_without_affecting_parent(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://proxy.invalid:7890", "HTTPS_PROXY": "http://proxy.invalid:7890"},
            clear=True,
        ):
            with patch.object(bootstrap, "probe_huggingface", side_effect=[False, True]):
                mode, env = bootstrap.resolve_hf_network(
                    Path(root), Path("/fixture/python"), offline=False
                )
            self.assertEqual(mode, "direct")
            self.assertNotIn("HTTP_PROXY", env)
            self.assertEqual(os.environ["HTTP_PROXY"], "http://proxy.invalid:7890")

    def test_huggingface_probe_uses_configured_endpoint(self) -> None:
        completed = type("Completed", (), {"returncode": 0})()
        with patch.object(bootstrap.subprocess, "run", return_value=completed) as run:
            self.assertTrue(
                bootstrap.probe_huggingface(
                    Path("/fixture/python"),
                    {"HF_ENDPOINT": "https://hf.example/"},
                )
            )
        self.assertEqual(
            run.call_args.args[0][-1],
            "https://hf.example/api/datasets/Tevatron/browsecomp-plus",
        )

    def test_managed_index_requires_a_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root)
            variant = "fixture-index"
            variant_root = cache / "indexes" / variant
            variant_root.mkdir(parents=True)
            first = variant_root / "corpus.shard1_of_2.pkl"
            first.write_bytes(b"first")
            pattern = str(variant_root / "corpus.shard*.pkl")
            commands: list[list[str]] = []

            def fake_execute(command: list[str], env=None) -> None:
                commands.append(command)
                second = variant_root / "corpus.shard2_of_2.pkl"
                second.write_bytes(b"second")
                (variant_root / bootstrap.INDEX_COMPLETE_MARKER).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "variant": variant,
                            "files": [
                                {"name": first.name, "size": first.stat().st_size},
                                {"name": second.name, "size": second.stat().st_size},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            with patch.object(bootstrap, "execute", side_effect=fake_execute):
                bootstrap.ensure_index(
                    cache,
                    pattern,
                    variant,
                    Path("/fixture/python"),
                    offline=False,
                    network_env={},
                )
                self.assertEqual(len(commands), 1)
                bootstrap.ensure_index(
                    cache,
                    pattern,
                    variant,
                    Path("/fixture/python"),
                    offline=False,
                    network_env={},
                )
            self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()
