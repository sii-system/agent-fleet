"""Tests for Pi Harbor integration and the shared Runfield runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        logs_dir: Path | None = None,
        **kwargs,
    ) -> None:
        self._version = version
        self.model_name = model_name
        self._extra_env = extra_env or {}
        self.thinking = thinking
        self.logs_dir = logs_dir
        self.agent_commands: list[dict[str, object]] = []
        self.root_commands: list[str] = []

    def version(self) -> str | None:
        return self._version

    def build_cli_flags(self) -> str:
        return ""

    def _build_register_skills_command(self) -> str | None:
        return None

    async def setup(self, environment) -> None:
        self.root_commands.append("[ -d /installed-agent ] || mkdir -p /installed-agent")
        if hasattr(environment, "exec"):
            await environment.exec(
                command="[ -d /installed-agent ] || mkdir -p /installed-agent",
                user="root",
            )
        if self.logs_dir is not None:
            (self.logs_dir / "setup").mkdir(parents=True, exist_ok=True)
        await self.install(environment)

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
        self.assertIn("/opt/agent-fleet/runtime/pi", command)
        self.assertIn('[ "$(cat "$shared_runtime/VERSION")" = "$pi_version" ]', command)
        self.assertIn('ln -sf "$shared_runtime/bin/pi"', command)
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

    def test_runfield_requires_shared_runtime_without_archive_fallback(self) -> None:
        agent = self.module.AgentFleetPi(version="1.3.0")

        with mock.patch.dict(
            "os.environ", {"HARBOR_BACKEND_PREINSTALLED_AGENTS": "1"}
        ):
            asyncio.run(agent.install(object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("shared_required=1", command)
        self.assertIn("required shared Pi runtime is unavailable", command)
        self.assertLess(
            command.index("required shared Pi runtime is unavailable"),
            command.index('elif [ -f "$node_tar" ]'),
        )

    def test_preinstalled_setup_issues_zero_rpcs_and_inlines_bootstrap_in_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            exec_calls: list[dict[str, object]] = []

            async def record_exec(**kwargs):
                exec_calls.append(kwargs)
                return SimpleNamespace(return_code=0, stdout="", stderr="")

            environment = SimpleNamespace(exec=record_exec)
            agent = self.module.AgentFleetPi(
                version="1.3.0",
                model_name="sii-gateway/test-model",
                extra_env={
                    "AGENT_FLEET_API_KEY": "fake-key",
                    "PI_MODELS_CONFIG": '{"providers":{}}',
                    "PI_SETTINGS_CONFIG": '{"thinking":"high"}',
                },
                logs_dir=logs_dir,
            )

            with mock.patch.dict(
                "os.environ", {"HARBOR_BACKEND_PREINSTALLED_AGENTS": "1"}
            ):
                asyncio.run(agent.setup(environment))
                self.assertEqual(exec_calls, [])
                self.assertEqual(agent.agent_commands, [])
                self.assertTrue((logs_dir / "setup").is_dir())
                self.assertTrue(agent._deferred_shared_setup)
                self.assertEqual(agent._version, "1.3.0")

                asyncio.run(agent.run("solve task", environment, object()))

            self.assertEqual(exec_calls, [])
            self.assertEqual(len(agent.agent_commands), 1)
            wrapped_cmd = str(agent.agent_commands[0]["command"])
            self.assertIn("pi_version=1.3.0", wrapped_cmd)
            self.assertIn(
                'mkdir -p /installed-agent "$HOME/.local/bin" "$HOME/.pi/agent"',
                wrapped_cmd,
            )
            self.assertIn(
                '[ -x "$shared_runtime/bin/node" ] && [ -x "$shared_runtime/bin/pi" ]',
                wrapped_cmd,
            )
            self.assertIn(
                '[ "$(cat "$shared_runtime/VERSION")" = "$pi_version" ]',
                wrapped_cmd,
            )
            self.assertIn(
                'required shared Pi runtime is unavailable or has the wrong version: $pi_version',
                wrapped_cmd,
            )
            self.assertIn('"$HOME/.pi/agent/models.json"', wrapped_cmd)
            self.assertIn('"$HOME/.pi/agent/settings.json"', wrapped_cmd)
            self.assertNotIn("node -p", wrapped_cmd)
            self.assertNotIn("pi --version", wrapped_cmd)
            self.assertLess(
                wrapped_cmd.index("models.json"),
                wrapped_cmd.index("ext_args=()"),
            )

            # Verify bootstrap shell snippet against matching and mismatched fake runtime dirs.
            runtime_root = Path(tmpdir) / "runtime"
            (runtime_root / "bin").mkdir(parents=True)
            (runtime_root / "bin/node").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (runtime_root / "bin/pi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (runtime_root / "bin/node").chmod(0o755)
            (runtime_root / "bin/pi").chmod(0o755)
            (runtime_root / "VERSION").write_text("1.3.0\n", encoding="utf-8")
            fake_home = Path(tmpdir) / "home"
            fake_installed = Path(tmpdir) / "installed-agent"
            bootstrap = agent._shared_runtime_bootstrap_command().replace(
                "/installed-agent", shlex.quote(str(fake_installed))
            )
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "PI_SHARED_RUNTIME_DIR": str(runtime_root),
                "PI_MODELS_CONFIG": '{"models":1}',
                "PI_SETTINGS_CONFIG": '{"settings":2}',
            }
            ok = subprocess.run(
                ["/bin/bash", "-c", bootstrap],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue((fake_home / ".local/bin/pi").is_symlink())
            self.assertEqual(
                (fake_home / ".pi/agent/models.json").read_text(encoding="utf-8"),
                '{"models":1}\n',
            )

            (runtime_root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            bad = subprocess.run(
                ["/bin/bash", "-c", bootstrap],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bad.returncode, 1)
            self.assertIn(
                "required shared Pi runtime is unavailable or has the wrong version: 1.3.0",
                bad.stderr,
            )

    def test_non_preinstalled_setup_delegates_to_super_without_inline_bootstrap(self) -> None:
        exec_calls: list[dict[str, object]] = []

        async def record_exec(**kwargs):
            exec_calls.append(kwargs)
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        environment = SimpleNamespace(exec=record_exec)
        agent = self.module.AgentFleetPi(
            version="0.81.1",
            model_name="sii-gateway/test-model",
        )
        with mock.patch.dict(
            "os.environ", {"HARBOR_BACKEND_PREINSTALLED_AGENTS": "0"}
        ):
            asyncio.run(agent.setup(environment))
            self.assertFalse(agent._deferred_shared_setup)
            self.assertEqual(len(exec_calls), 1)
            self.assertEqual(len(agent.agent_commands), 1)
            asyncio.run(agent.run("solve task", object(), object()))
            run_cmd = str(agent.agent_commands[-1]["command"])
            self.assertNotIn("required shared Pi runtime is unavailable", run_cmd)

    def test_run_uses_custom_provider_and_forwards_extra_environment(self) -> None:
        agent = self.module.AgentFleetPi(
            model_name="sii-gateway/test-model",
            extra_env={"AGENT_FLEET_API_KEY": "fake-key"},
            thinking="high",
        )

        instruction = "- solve 'this' task"
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

    def test_run_rejects_model_without_provider(self) -> None:
        agent = self.module.AgentFleetPi(model_name="test-model")

        with self.assertRaisesRegex(ValueError, "provider/model_name"):
            asyncio.run(agent.run("solve", object(), object()))

    def test_build_cli_flags_is_empty(self) -> None:
        # Pi extension -e flags are built inside the container (the mount is
        # only visible there), so the wrapper must not try to emit them.
        agent = self.module.AgentFleetPi()
        self.assertEqual(agent.build_cli_flags(), "")

    def test_run_collects_extensions_inside_container(self) -> None:
        agent = self.module.AgentFleetPi(
            model_name="sii-gateway/test-model",
            extra_env={"PI_EXTENSION_DIR": "/opt/tb-pi/extensions"},
        )

        asyncio.run(agent.run("- solve task", object(), object()))

        command = str(agent.agent_commands[-1]["command"])
        self.assertIn("ext_args=()", command)
        self.assertIn('"$PI_EXTENSION_DIR"/*.ts', command)
        self.assertIn('ext_args+=( -e "$ext" )', command)
        self.assertIn('"${ext_args[@]:-}"', command)
        # provider/model still present after the pi invocation
        self.assertIn("--provider sii-gateway", command)
        self.assertIn("--model test-model", command)


if __name__ == "__main__":
    unittest.main()
