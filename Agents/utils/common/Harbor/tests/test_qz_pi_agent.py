"""Unit tests for the qz Pi agent (models.json + runtime-source contracts)."""

import asyncio
import importlib.util
import json
import os
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = HARBOR_DIR / "qz_pi_agent.py"
sys.path.insert(0, str(HARBOR_DIR))


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    agents = types.ModuleType("harbor.agents")
    installed = types.ModuleType("harbor.agents.installed")
    base = types.ModuleType("harbor.agents.installed.base")
    pi = types.ModuleType("harbor.agents.installed.pi")
    environments = types.ModuleType("harbor.environments")
    environments_base = types.ModuleType("harbor.environments.base")
    models = types.ModuleType("harbor.models")
    models_agent = types.ModuleType("harbor.models.agent")
    context = types.ModuleType("harbor.models.agent.context")

    def with_prompt_template(fn):
        return fn

    base.with_prompt_template = with_prompt_template

    class StubPi:
        _OUTPUT_FILENAME = "pi.txt"

        def __init__(self, *args, model_name: str = "", version: str = "", **kwargs):
            self.model_name = model_name
            self._version = version
            self.root_calls: list[tuple[str | None, dict | None]] = []
            self.agent_calls: list[tuple[str | None, dict | None]] = []

        def build_cli_flags(self) -> str:
            return ""

        async def exec_as_root(self, environment, command=None, env=None, **kwargs):
            self.root_calls.append((command, env))

        async def exec_as_agent(self, environment, command=None, env=None, **kwargs):
            self.agent_calls.append((command, env))

    pi.Pi = StubPi

    class BaseEnvironment:
        pass

    environments_base.BaseEnvironment = BaseEnvironment

    class AgentContext:
        pass

    context.AgentContext = AgentContext

    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.agents": agents,
            "harbor.agents.installed": installed,
            "harbor.agents.installed.base": base,
            "harbor.agents.installed.pi": pi,
            "harbor.environments": environments,
            "harbor.environments.base": environments_base,
            "harbor.models": models,
            "harbor.models.agent": models_agent,
            "harbor.models.agent.context": context,
        }
    )


def load_module():
    install_harbor_stubs()
    spec = importlib.util.spec_from_file_location("qz_pi_agent", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def make_agent(**kwargs):
    kwargs.setdefault("model_name", "glm-test")
    kwargs.setdefault("base_url", "http://gw.example")
    kwargs.setdefault("api_key", "sk-tail=")
    return MODULE.QzPi(**kwargs)


class RecordingEnvironment:
    default_user = "sandbox"

    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []

    async def upload_file(self, source_path, target_path):
        self.uploads.append((target_path, Path(source_path).read_bytes()))


class QzPiInitTest(unittest.TestCase):
    def test_appends_v1_to_bare_base_url(self) -> None:
        agent = make_agent(base_url="http://gw.example")
        self.assertEqual(agent._qz_base_url, "http://gw.example/v1")

    def test_keeps_existing_v1_and_strips_trailing_slash(self) -> None:
        agent = make_agent(base_url="http://gw.example/v1/")
        self.assertEqual(agent._qz_base_url, "http://gw.example/v1")

    def test_falls_back_to_process_env(self) -> None:
        env = {"BASE_URL": "http://env-gw.example", "API_KEY": "env-key"}
        with patch.dict(os.environ, env):
            agent = MODULE.QzPi(model_name="glm-test", base_url="", api_key="")
        self.assertEqual(agent._qz_base_url, "http://env-gw.example/v1")
        self.assertEqual(agent._qz_api_key, "env-key")

    def test_requires_base_url_and_api_key(self) -> None:
        with patch.dict(os.environ, {"BASE_URL": "", "API_KEY": ""}):
            with self.assertRaises(ValueError):
                MODULE.QzPi(model_name="glm-test", base_url="", api_key="k")
            with self.assertRaises(ValueError):
                MODULE.QzPi(model_name="glm-test", base_url="http://gw", api_key="")


class QzPiInstallTest(unittest.TestCase):
    def test_defaults_node_and_pi_to_npmmirror(self) -> None:
        with patch.dict(
            os.environ,
            {
                "QZ_NODE_DIST_URL": "",
                "HARBOR_CC_NODE_DIST_URL": "",
                "NPM_CONFIG_REGISTRY": "",
            },
        ):
            agent = make_agent()
        asyncio.run(agent.install(object()))
        self.assertEqual(len(agent.root_calls), 1)
        command = agent.root_calls[0][0]
        self.assertIn("registry.npmmirror.com/-/binary/node/", command)
        self.assertIn(f"--registry {MODULE.DEFAULT_NPM_REGISTRY}", command)
        self.assertIn("@mariozechner/pi-coding-agent@latest", command)

    def test_uses_configured_generic_runtime_sources(self) -> None:
        node_dist_url = (
            "https://nodejs.org/dist/v22.14.0/"
            "node-v22.14.0-linux-x64.tar.gz"
        )
        npm_registry = "https://registry.npmjs.org"
        with patch.dict(
            os.environ,
            {
                "QZ_NODE_DIST_URL": "",
                "HARBOR_CC_NODE_DIST_URL": node_dist_url,
                "NPM_CONFIG_REGISTRY": npm_registry,
            },
        ):
            agent = make_agent()

        asyncio.run(agent.install(object()))
        command = agent.root_calls[0][0]
        self.assertIn(f"curl -fsSL {node_dist_url}", command)
        self.assertIn(f"--registry {npm_registry}", command)

    def test_qz_node_source_and_generic_npm_source_can_be_combined(self) -> None:
        with patch.dict(
            os.environ,
            {
                "QZ_NODE_DIST_URL": "https://qz.example/node.tgz",
                "HARBOR_CC_NODE_DIST_URL": "https://generic.example/node.tgz",
                "NPM_CONFIG_REGISTRY": "https://generic.example/npm",
            },
        ):
            agent = make_agent()

        self.assertEqual(agent._node_dist_url, "https://qz.example/node.tgz")
        self.assertEqual(agent._npm_registry, "https://generic.example/npm")

    def test_pins_version_when_provided(self) -> None:
        agent = make_agent(version="0.55.1")
        asyncio.run(agent.install(object()))
        self.assertIn(
            "@mariozechner/pi-coding-agent@0.55.1", agent.root_calls[0][0]
        )


class QzPiModelsConfigTest(unittest.TestCase):
    def test_provider_contract(self) -> None:
        agent = make_agent()
        config = agent._models_config()
        provider = config["providers"][MODULE.PROVIDER]
        self.assertEqual(provider["baseUrl"], "http://gw.example/v1")
        # The sandbox pi build does not resolve "$VAR" apiKey references, so
        # the literal key must land in models.json.
        self.assertEqual(provider["apiKey"], "sk-tail=")
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["models"][0]["id"], "glm-test")


class QzPiRunTest(unittest.TestCase):
    def test_writes_models_json_then_runs_pi(self) -> None:
        agent = make_agent()
        environment = RecordingEnvironment()
        asyncio.run(agent.run("do the task", environment, object()))
        self.assertEqual(len(agent.agent_calls), 2)

        setup_command, setup_env = agent.agent_calls[0]
        self.assertIn(f"mkdir -p {MODULE.AGENT_DIR}", setup_command)
        self.assertIsNone(setup_env)

        self.assertEqual(len(environment.uploads), 1)
        target_path, payload = environment.uploads[0]
        self.assertEqual(target_path, MODULE.MODELS_PATH)
        config = json.loads(payload)
        self.assertEqual(config["providers"][MODULE.PROVIDER]["apiKey"], "sk-tail=")

        self.assertEqual(len(agent.root_calls), 1)
        ownership_command, ownership_env = agent.root_calls[0]
        self.assertEqual(
            ownership_command,
            f"chown sandbox {MODULE.MODELS_PATH} && chmod 600 {MODULE.MODELS_PATH}",
        )
        self.assertIsNone(ownership_env)

        run_command, run_env = agent.agent_calls[1]
        self.assertTrue(run_command.startswith("set -o pipefail; pi --print"))
        self.assertIn(f"--provider {MODULE.PROVIDER}", run_command)
        self.assertIn("--model glm-test", run_command)
        self.assertIn(shlex.quote("do the task"), run_command)
        self.assertIn(f"/logs/agent/{agent._OUTPUT_FILENAME}", run_command)
        self.assertEqual(run_env["PI_CODING_AGENT_DIR"], MODULE.AGENT_DIR)
        self.assertEqual(run_env["PI_OFFLINE"], "1")
        for command, env in agent.agent_calls + agent.root_calls:
            self.assertNotIn("sk-tail=", command or "")
            self.assertNotIn("sk-tail=", json.dumps(env or {}))

    def test_requires_model_name(self) -> None:
        agent = make_agent(model_name="")
        with self.assertRaises(ValueError):
            asyncio.run(agent.run("do the task", object(), object()))


if __name__ == "__main__":
    unittest.main()
