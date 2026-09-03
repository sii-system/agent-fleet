from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from dsh_sdk_minimal_harbor import AgentFleetDshSdkMinimal  # noqa: E402


class AgentFleetDshSdkMinimalTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, root: Path, **kwargs: Any) -> AgentFleetDshSdkMinimal:
        return AgentFleetDshSdkMinimal(
            logs_dir=root,
            version="0.1.2-alpha.2",
            model_name="deepseek/private/deepseek-v4-flash-0731",
            extra_env={
                "DSH_API_KEY": "fake-key",
                "DSH_BASE_URL": "https://gateway.example.test/v1",
                "DSH_PYTHON_RUNTIME_PATH": "/cache/python.tar.gz",
                "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH": "/cache/sdk.tar.gz",
                "DSH_NODE_RUNTIME_PATH": "/cache/node.tar.gz",
                "DSH_CLI_RUNTIME_PATH": "/cache/dsh.tar.gz",
            },
            **kwargs,
        )

    def test_runtime_env_selects_profile_home_and_sampling_relay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            env = self.make_agent(Path(temporary_name))._runtime_env()

        self.assertEqual(env["DSH_HOME"], "/logs/agent/dsh-home")
        self.assertEqual(env["DSH_MODEL"], "private/deepseek-v4-flash-0731")
        self.assertEqual(env["DSH_CONTEXT_WINDOW"], "200000")
        self.assertEqual(env["DEEPSEEK_BASE_URL"], "http://127.0.0.1:18100/v1")
        self.assertEqual(env["GIT_PAGER"], "cat")
        self.assertEqual(env["GIT_EDITOR"], "true")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(
            env["DSH_SAMPLING_UPSTREAM_BASE_URL"],
            "https://gateway.example.test/v1",
        )

    async def test_install_uses_four_offline_archives_and_checks_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            environment = AsyncMock()
            environment.default_user = "agent"
            agent.exec_as_root = AsyncMock()
            agent.exec_as_agent = AsyncMock()

            await agent.install(environment)

        root_command = agent.exec_as_root.await_args.kwargs["command"]
        for variable in (
            "DSH_PYTHON_RUNTIME_PATH",
            "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH",
            "DSH_NODE_RUNTIME_PATH",
            "DSH_CLI_RUNTIME_PATH",
        ):
            self.assertIn(variable, root_command)
        commands = "\n".join(
            call.kwargs["command"] for call in agent.exec_as_agent.await_args_list
        )
        self.assertIn("dsh --profile sdk-minimal --dump-config", commands)
        self.assertNotIn("pip install", commands)
        self.assertNotIn("curl", commands)

    async def test_run_uses_new_sdk_profile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name), max_tokens="49152")
            environment = AsyncMock()
            agent.exec_as_agent = AsyncMock()

            await agent.run("fix the tests", environment, AsyncMock())

        call = agent.exec_as_agent.await_args
        command = call.kwargs["command"]
        self.assertIn("/installed-agent/sdk_minimal.py", command)
        self.assertIn("--profile sdk-minimal", command)
        self.assertIn("--dsh-home /logs/agent/dsh-home", command)
        self.assertIn('--dsh-bin "$HOME/.local/bin/dsh"', command)
        self.assertIn("--reasoning-effort max", command)
        self.assertIn("--max-tokens 49152", command)
        self.assertIn("--trace-path /logs/agent/dsh-sdk-minimal-trace.jsonl", command)
        self.assertIn("dsh_sampling_relay.py", command)
        self.assertIn("command -v stdbuf", command)
        self.assertIn('else\n    tee "$@"', command)
        self.assertEqual(call.kwargs["env"]["DSH_HOME"], "/logs/agent/dsh-home")


if __name__ == "__main__":
    unittest.main()
