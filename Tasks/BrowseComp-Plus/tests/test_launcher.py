from __future__ import annotations

import importlib.util
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

BENCHMARK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK / "scripts"))
sys.path.insert(0, str(BENCHMARK))
SPEC = importlib.util.spec_from_file_location(
    "browsecomp_launcher", BENCHMARK / "mcp" / "launcher.py"
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTest(unittest.TestCase):
    def test_local_probe_uses_a_proxy_free_opener(self) -> None:
        opener = type(
            "FixtureOpener",
            (),
            {
                "open": lambda self, request, timeout: (_ for _ in ()).throw(
                    urllib.error.URLError("not listening")
                )
            },
        )()
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener) as build:
            self.assertFalse(launcher.probe("http://127.0.0.1:8000/mcp"))
        self.assertEqual(build.call_count, 1)

    def test_default_port_skips_an_unmanaged_listener_and_is_persisted(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            blocked_port = occupied.getsockname()[1]
            with tempfile.TemporaryDirectory() as root:
                state = Path(root)
                selected = launcher.choose_default_port(state, blocked_port)
                self.assertNotEqual(selected, blocked_port)
                self.assertEqual(
                    int((state / "default-port").read_text().strip()), selected
                )
                self.assertEqual(
                    launcher.choose_default_port(state, blocked_port), selected
                )

    def test_remote_embedding_can_keep_an_inherited_proxy(self) -> None:
        with patch.dict(
            launcher.os.environ,
            {
                "BROWSECOMP_EMBEDDING_BACKEND": "openai",
                "BROWSECOMP_EMBEDDING_BASE_URL": "https://embed.example/v1",
                "BROWSECOMP_EMBEDDING_PROXY_MODE": "inherit",
                "BROWSECOMP_HF_PROXY_MODE_RESOLVED": "direct",
                "HTTPS_PROXY": "http://proxy.example:8080",
            },
            clear=True,
        ):
            environment = launcher.retriever_environment()
        self.assertEqual(environment["HTTPS_PROXY"], "http://proxy.example:8080")

    def test_remote_embedding_direct_mode_bypasses_proxy_for_its_host(self) -> None:
        with patch.dict(
            launcher.os.environ,
            {
                "BROWSECOMP_EMBEDDING_BACKEND": "openai",
                "BROWSECOMP_EMBEDDING_BASE_URL": "https://embed.example/v1",
                "BROWSECOMP_HF_PROXY_MODE_RESOLVED": "direct",
                "HTTPS_PROXY": "http://proxy.example:8080",
            },
            clear=True,
        ):
            environment = launcher.retriever_environment()
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertIn("embed.example", environment["NO_PROXY"])

    def test_legacy_local_command_is_equivalent_to_new_default(self) -> None:
        legacy = [
            "python",
            "server.py",
            "--model-name",
            "Qwen/Qwen3-Embedding-0.6B",
            "--model-revision",
            "revision",
            "--dataset-revision",
            "dataset",
        ]
        requested = legacy[:6] + [
            "--embedding-backend",
            "local",
            "--embedding-api-key-env",
            "API_KEY",
            "--embedding-api-model",
            "Qwen/Qwen3-Embedding-0.6B",
            "--embedding-api-timeout-seconds",
            "60.0",
            "--embedding-api-max-retries",
            "2",
            "--tokenizer-model",
            "Qwen/Qwen3-Embedding-0.6B",
            "--tokenizer-revision",
            "revision",
        ] + legacy[6:]
        self.assertTrue(launcher.commands_equivalent(legacy, requested))
        remote = requested.copy()
        remote[remote.index("local")] = "openai"
        self.assertFalse(launcher.commands_equivalent(legacy, remote))

    def test_port_selection_skips_managed_different_retriever(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "mcp-8000.pid").write_text("1234\n", encoding="utf-8")
            (state / "mcp-8000.command.json").write_text(
                json.dumps(["python", "server.py", "--embedding-backend", "local"]),
                encoding="utf-8",
            )
            requested = ["python", "server.py", "--embedding-backend", "openai"]
            with (
                patch.object(launcher, "is_alive", return_value=True),
                patch.object(launcher, "port_available", return_value=True),
            ):
                selected = launcher.choose_default_port(state, 8000, requested)
            self.assertEqual(selected, 8001)

    def test_port_selection_prefers_matching_warm_retriever(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "default-port").write_text("8001\n", encoding="utf-8")
            command = ["python", "server.py", "--embedding-backend", "openai"]
            (state / "mcp-8000.pid").write_text("1234\n", encoding="utf-8")
            (state / "mcp-8000.command.json").write_text(
                json.dumps(command), encoding="utf-8"
            )
            with patch.object(launcher, "is_alive", return_value=True):
                selected = launcher.choose_default_port(state, 8000, command)
            self.assertEqual(selected, 8000)

    def test_port_selection_reuses_matching_retriever_on_a_shifted_port(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "default-port").write_text("8001\n", encoding="utf-8")
            existing = [
                "python",
                "server.py",
                "--port",
                "8001",
                "--embedding-backend",
                "openai",
            ]
            requested = existing.copy()
            requested[requested.index("8001")] = "8000"
            (state / "mcp-8001.pid").write_text("1234\n", encoding="utf-8")
            (state / "mcp-8001.command.json").write_text(
                json.dumps(existing), encoding="utf-8"
            )
            with patch.object(launcher, "is_alive", return_value=True):
                selected = launcher.choose_default_port(state, 8000, requested)
            self.assertEqual(selected, 8001)
            self.assertTrue(launcher.commands_equivalent(existing, requested))


if __name__ == "__main__":
    unittest.main()
