from __future__ import annotations

import asyncio
import importlib.util
import os
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

MODULE_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakePi:
    _OUTPUT_FILENAME = "pi.txt"

    def __init__(
        self,
        *args,
        version: str | None = None,
        model_name: str | None = None,
        extra_env: dict[str, str] | None = None,
        thinking: str | None = None,
        **kwargs,
    ) -> None:
        self._version = version
        self.model_name = model_name
        self._extra_env = extra_env or {}
        self.thinking = thinking
        self.agent_commands: list[dict[str, object]] = []

    def version(self) -> str | None:
        return self._version

    def build_cli_flags(self) -> str:
        return ""

    def _build_register_skills_command(self) -> str | None:
        return None

    async def exec_as_agent(self, environment, **kwargs) -> None:
        self.agent_commands.append(kwargs)


def make_harbor_stubs() -> dict[str, types.ModuleType]:
    stubs: dict[str, types.ModuleType] = {}
    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.environments",
        "harbor.models",
        "harbor.models.agent",
    ):
        module = types.ModuleType(name)
        module.__path__ = []
        stubs[name] = module

    installed_base = types.ModuleType("harbor.agents.installed.base")
    installed_base.with_prompt_template = lambda function: function
    stubs[installed_base.__name__] = installed_base

    installed_pi = types.ModuleType("harbor.agents.installed.pi")
    installed_pi.Pi = FakePi
    stubs[installed_pi.__name__] = installed_pi

    environments_base = types.ModuleType("harbor.environments.base")
    environments_base.BaseEnvironment = object
    stubs[environments_base.__name__] = environments_base

    context = types.ModuleType("harbor.models.agent.context")
    context.AgentContext = object
    stubs[context.__name__] = context
    return stubs


class AgentFleetPiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_name = "test_agent_fleet_pi_harbor"
        with mock.patch.dict(sys.modules, make_harbor_stubs()):
            cls.module = load_module(
                cls.module_name,
                MODULE_DIR / "pi_harbor.py",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop(cls.module_name, None)

    def test_install_uses_pinned_fork_and_cache_first_runtime(self) -> None:
        agent = self.module.AgentFleetPi(version="0.81.1")

        asyncio.run(agent.install(object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("pi-node-runtime.tar.gz", command)
        self.assertIn('if [ -f "$node_tar" ]; then', command)
        self.assertIn('tar -xzf "$node_tar"', command)
        self.assertIn("Pi setup requires Node 20 or newer", command)
        self.assertIn("pi-runtime-${pi_version}.tar.gz", command)
        self.assertIn('tar -xzf "$pi_runtime_tar"', command)
        self.assertNotIn("npm install", command)
        self.assertNotIn("npm-cache", command)
        self.assertIn('"$HOME/.pi/agent/models.json"', command)
        self.assertIn('"$HOME/.pi/agent/settings.json"', command)
        self.assertIn('mkdir -p "$HOME/.local/bin" "$HOME/.pi/agent"', command)
        # /logs/agent is a Harbor-mounted dir; never let a read-only or
        # non-root container abort pi setup over it.
        self.assertIn('mkdir -p "/logs/agent" 2>/dev/null || echo', command)
        self.assertNotIn("nvm", command)
        self.assertNotIn("@mariozechner", command)

    def test_run_uses_custom_provider_and_forwards_extra_environment(self) -> None:
        agent = self.module.AgentFleetPi(
            model_name="test-model",
            extra_env={"AGENT_FLEET_API_KEY": "fake-key"},
            thinking="high",
        )

        instruction = "- solve 'this' task"
        with mock.patch.dict(os.environ, {"PI_PROVIDER": "sii-gateway"}):
            asyncio.run(agent.run(instruction, object(), object()))

        invocation = agent.agent_commands[-1]
        command = str(invocation["command"])
        self.assertIn("pi --print --mode json --no-session", command)
        self.assertIn("--provider sii-gateway", command)
        self.assertIn("--model test-model", command)
        # Thinking strength rides PI_SETTINGS_CONFIG (settings.json), not a
        # wrapper CLI flag, mirroring opencode's config-driven thinking.
        self.assertNotIn("--thinking", command)
        self.assertIn("/logs/agent/pi.txt", command)
        self.assertIn(
            f"printf '%s' {shlex.quote(instruction)} | pi --print",
            command,
        )
        # A successful pi run may emit only message_update lines; the grep -v
        # must not fail the pipeline under set -o pipefail.
        self.assertIn("{ grep -v", command)
        self.assertIn("|| true; }", command)
        # stdbuf may be missing in minimal task images; the pipeline must
        # fall back to a plain tee instead of failing the whole run.
        self.assertIn("command -v stdbuf", command)
        self.assertIn("stdbuf -oL tee", command)
        self.assertIn("else tee /logs/agent/pi.txt", command)
        self.assertNotIn("</dev/null", command)
        self.assertEqual(
            invocation["env"],
            {"AGENT_FLEET_API_KEY": "fake-key", "PI_OFFLINE": "1"},
        )

    def test_run_rejects_missing_provider(self) -> None:
        agent = self.module.AgentFleetPi(model_name="test-model")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "PI_PROVIDER": "",
                    "HARBOR_ANTHROPIC_BASE_URL": "",
                    "BASE_URL": "",
                },
            ),
            self.assertRaisesRegex(ValueError, "PI_PROVIDER must not be empty"),
        ):
            asyncio.run(agent.run("solve", object(), object()))

    def test_run_supports_legacy_provider_model_name(self) -> None:
        agent = self.module.AgentFleetPi(model_name="legacy-provider/test-model")

        with mock.patch.dict(
            os.environ,
            {
                "PI_PROVIDER": "",
                "HARBOR_ANTHROPIC_BASE_URL": "",
                "BASE_URL": "",
            },
        ):
            asyncio.run(agent.run("solve", object(), object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("--provider legacy-provider", command)
        self.assertIn("--model test-model", command)

    def test_run_derives_provider_from_base_url(self) -> None:
        agent = self.module.AgentFleetPi(model_name="test-model")

        with mock.patch.dict(
            os.environ,
            {
                "PI_PROVIDER": "",
                "HARBOR_ANTHROPIC_BASE_URL": "",
                "BASE_URL": "https://Gateway.Example.com/v1",
            },
        ):
            asyncio.run(agent.run("solve", object(), object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("--provider gateway.example.com", command)
        self.assertIn("--model test-model", command)

    def test_run_preserves_slashes_in_model_id(self) -> None:
        model = "m-20260820192358-jsrtc/deepseekv4-flash-0731"
        agent = self.module.AgentFleetPi(model_name=model)

        with mock.patch.dict(os.environ, {"PI_PROVIDER": "sii-gateway"}):
            asyncio.run(agent.run("solve", object(), object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("--provider sii-gateway", command)
        self.assertIn(f"--model {model}", command)

    def test_build_cli_flags_is_empty(self) -> None:
        # Pi extension -e flags are built inside the container (the mount is
        # only visible there), so the wrapper must not try to emit them.
        agent = self.module.AgentFleetPi()
        self.assertEqual(agent.build_cli_flags(), "")

    def test_run_collects_extensions_inside_container(self) -> None:
        agent = self.module.AgentFleetPi(
            model_name="test-model",
            extra_env={"PI_EXTENSION_DIR": "/opt/tb-pi/extensions"},
        )

        with mock.patch.dict(os.environ, {"PI_PROVIDER": "sii-gateway"}):
            asyncio.run(agent.run("- solve task", object(), object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("ext_args=()", command)
        self.assertIn('"$PI_EXTENSION_DIR"/*.ts', command)
        self.assertIn('ext_args+=( -e "$ext" )', command)
        self.assertIn('"${ext_args[@]:-}"', command)
        # Provider and opaque model ID are still present after the pi invocation.
        self.assertIn("--provider sii-gateway", command)
        self.assertIn("--model test-model", command)


if __name__ == "__main__":
    unittest.main()
