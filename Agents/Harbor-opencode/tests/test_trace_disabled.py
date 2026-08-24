from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

MODULE_DIR = Path(__file__).resolve().parents[1]
HARBOR_RUNTIME_DIR = MODULE_DIR.parent / "utils" / "common" / "Harbor"


def load_module(name: str, path: Path):
    sys.path.insert(0, str(HARBOR_RUNTIME_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HARBOR_RUNTIME_DIR))


class FakeOpenCode:
    def __init__(
        self,
        *args,
        model_name: str | None = None,
        extra_env: dict[str, str] | None = None,
        fake_opencode_present: bool = True,
        **kwargs,
    ) -> None:
        self.model_name = model_name
        self._extra_env = extra_env or {}
        self.fake_opencode_present = fake_opencode_present
        self.root_commands: list[dict[str, object]] = []
        self.agent_commands: list[dict[str, object]] = []

    @property
    def extra_env(self) -> dict[str, str]:
        return self._extra_env

    async def exec_as_root(self, environment, **kwargs) -> None:
        self.root_commands.append(kwargs)

    async def exec_as_agent(self, environment, **kwargs) -> None:
        self.agent_commands.append(kwargs)
        command = str(kwargs.get("command", ""))
        if hasattr(environment, "exec"):
            await environment.exec(command=command, env=kwargs.get("env"))
        if not self.fake_opencode_present and "node --version" in command:
            raise RuntimeError("opencode is not installed")

    def _build_register_skills_command(self):
        return None

    def _build_register_config_command(self):
        return None


class FakeEnvironment:
    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []

    async def upload_file(self, source: Path, destination: str) -> None:
        self.uploads.append((source, destination))
        if not source.is_file():
            raise FileNotFoundError(source)


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

    installed_opencode = types.ModuleType("harbor.agents.installed.opencode")
    installed_opencode.OpenCode = FakeOpenCode
    stubs[installed_opencode.__name__] = installed_opencode

    environments_base = types.ModuleType("harbor.environments.base")
    environments_base.BaseEnvironment = object
    stubs[environments_base.__name__] = environments_base

    context = types.ModuleType("harbor.models.agent.context")
    context.AgentContext = object
    stubs[context.__name__] = context
    return stubs


class OpenCodeTraceDisabledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_name = "test_opik_opencode_harbor"
        with mock.patch.dict(sys.modules, make_harbor_stubs()):
            cls.module = load_module(
                cls.module_name,
                MODULE_DIR / "opik_opencode_harbor.py",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop(cls.module_name, None)

    def make_agent(self, trace: str, *, opencode_present: bool = True):
        # OPIK_URL is the single switch, so "tracing off" means the host never
        # forwarded an endpoint into the container environment.
        traced = trace not in {"false", "0", ""}
        return self.module.OpikOpenCodeHarbor(
            logs_dir=Path("/tmp/test-opencode-logs"),
            model_name="custom/test-model",
            extra_env={
                "CC_NODE_DIST_URL": (
                    "https://registry.npmmirror.com/-/binary/node/"
                    "v22.14.0/node-v22.14.0-linux-x64.tar.gz"
                ),
                "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
                "OPIK_URL": "http://localhost:5173" if traced else "",
                "OPIK_URL_OVERRIDE": (
                    "http://localhost:5173/api" if traced else ""
                ),
            },
            fake_opencode_present=opencode_present,
        )

    def test_trace_switch_matches_shell_semantics(self) -> None:
        self.assertFalse(self.module.opik_tracing_enabled({"OPIK_URL": ""}))
        self.assertFalse(self.module.opik_tracing_enabled({"OPIK_URL": "   "}))
        self.assertTrue(
            self.module.opik_tracing_enabled(
                {"OPIK_URL": "https://opik.example.invalid/api"}
            )
        )
        # A retired switch must not turn tracing back on.
        self.assertFalse(
            self.module.opik_tracing_enabled(
                {"OPIK_URL": "", "TRACE_TO_OPIK": "true"}
            )
        )

    def test_trace_switch_honors_disable_truth_table(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                self.assertFalse(
                    self.module.opik_tracing_enabled(
                        {
                            "OPIK_URL": "https://opik.example.invalid/api",
                            "OPIK_TRACK_DISABLE": value,
                        }
                    )
                )
        for value in ("", "0", "false", "no", "off", "unexpected"):
            with self.subTest(value=value):
                self.assertTrue(
                    self.module.opik_tracing_enabled(
                        {
                            "OPIK_URL": "https://opik.example.invalid/api",
                            "OPIK_TRACK_DISABLE": value,
                        }
                    )
                )

    def test_install_skips_opik_dependencies_and_missing_plugin_files(self) -> None:
        agent = self.make_agent("false")
        environment = FakeEnvironment()

        asyncio.run(agent.install(environment))

        self.assertEqual(environment.uploads, [])
        commands = "\n".join(
            str(item.get("command", "")) for item in agent.agent_commands
        )
        self.assertNotIn("mods = ('opik', 'uuid6', 'socksio')", commands)
        self.assertNotIn("opik-trace.ts", commands)

    def test_install_uses_sandbox_reachable_node_dist_before_apt(self) -> None:
        agent = self.make_agent("false", opencode_present=False)

        asyncio.run(agent.install(FakeEnvironment()))

        install_command = next(
            str(item.get("command", ""))
            for item in agent.agent_commands
            if "tool_executable=opencode" in str(item.get("command", ""))
        )
        self.assertIn("node_dist_url=https://registry.npmmirror.com", install_command)
        self.assertIn(
            'if download_file "$node_dist_url" "$node_dist_tgz" '
            '&& [ -s "$node_dist_tgz" ]; then',
            install_command,
        )
        self.assertIn(
            'if extract_archive "$node_dist_tgz" "$node_dir"; then',
            install_command,
        )
        self.assertIn("Acquire::ForceIPv4=true", install_command)
        self.assertIn("npm install -g --offline --cache", install_command)
        self.assertLess(
            install_command.index("node_dist_url="),
            install_command.index("apt-get -o Acquire::ForceIPv4=true update"),
        )
        self.assertIn("npm install -g opencode-ai@latest", install_command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=install_command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_install_trace_on_keeps_opik_dependencies_and_plugin_files(self) -> None:
        agent = self.make_agent("true")
        environment = FakeEnvironment()

        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "opik-trace.ts"
            hook = Path(tmp) / "opencode_realtime_trace.py"
            plugin.touch()
            hook.touch()
            with (
                mock.patch.object(self.module, "PLUGIN_TS", plugin),
                mock.patch.object(self.module, "HOOK_PY", hook),
            ):
                asyncio.run(agent.install(environment))

        destinations = [destination for _, destination in environment.uploads]
        self.assertEqual(
            destinations,
            [
                "/tmp/opik-trace.ts",
                "/tmp/opencode_realtime_trace.py",
                "/tmp/finalize_opencode_sessions.py",
                "/tmp/opik_trace_gate.py",
            ],
        )
        commands = "\n".join(
            str(item.get("command", "")) for item in agent.agent_commands
        )
        self.assertIn("mods = ('opik', 'uuid6', 'socksio')", commands)

    def test_runtime_disable_skips_trace_install_and_run(self) -> None:
        agent = self.make_agent("true")
        environment = FakeEnvironment()

        with mock.patch.dict(os.environ, {"OPIK_TRACK_DISABLE": "true"}):
            asyncio.run(agent.install(environment))
            asyncio.run(agent.run("solve the task", environment, object()))

        self.assertEqual(environment.uploads, [])
        commands = "\n".join(
            str(item.get("command", "")) for item in agent.agent_commands
        )
        self.assertNotIn("opik-trace.ts", commands)
        self.assertNotIn("finalize_opencode_sessions.py", commands)

    def test_run_skips_plugin_registration_and_finalizer(self) -> None:
        agent = self.make_agent("false")

        asyncio.run(agent.run("solve the task", FakeEnvironment(), object()))

        commands = "\n".join(
            str(item.get("command", "")) for item in agent.agent_commands
        )
        self.assertNotIn("opik-trace.ts", commands)
        self.assertNotIn("finalize_opencode_sessions.py", commands)
        self.assertNotIn(
            "OC_OPIK_LOGS_DIR",
            agent.agent_commands[-1].get("env", {}),
        )
        self.assertNotIn(
            "OPENCODE_FAKE_VCS",
            agent.agent_commands[-1].get("env", {}),
        )

    def test_run_trace_on_keeps_plugin_registration_and_finalizer(self) -> None:
        agent = self.make_agent("true")

        asyncio.run(agent.run("solve the task", FakeEnvironment(), object()))

        commands = "\n".join(
            str(item.get("command", "")) for item in agent.agent_commands
        )
        self.assertIn("opik-trace.ts", commands)
        self.assertIn("finalize_opencode_sessions.py", commands)
        run_env = agent.agent_commands[-1].get("env", {})
        self.assertEqual(run_env.get("OC_OPIK_LOGS_DIR"), "/logs/agent")
        self.assertEqual(
            run_env.get("OPIK_URL"),
            "http://host.docker.internal:5173/api/",
        )

    def test_runtime_secrets_use_trial_scope_not_per_command_env(self) -> None:
        runtime_secrets = {
            "ANTHROPIC_API_KEY": "fake-runtime-secret",
            "AGENT_FLEET_OPENCODE_SECRET_0123456789ABCDEF": (
                "fake-runtime-secret"
            ),
        }
        with mock.patch.object(
            self.module,
            "OPENCODE_RUNTIME_SECRETS",
            runtime_secrets,
        ):
            agent = self.make_agent("false")

        self.assertEqual(
            {key: agent._extra_env[key] for key in runtime_secrets},
            runtime_secrets,
        )

        asyncio.run(agent.run("solve the task", FakeEnvironment(), object()))

        for command in agent.agent_commands:
            command_env = command.get("env", {})
            self.assertTrue(runtime_secrets.keys().isdisjoint(command_env))
            self.assertNotIn(
                "fake-runtime-secret",
                str(command.get("command", "")),
            )
            self.assertNotIn("fake-runtime-secret", command_env.values())

    def test_runtime_secrets_reach_environment_exec_through_harbor_scope(self) -> None:
        try:
            from harbor.environments.base import BaseEnvironment
            from harbor.trial.single_step import SingleStepTrial
        except ImportError:
            self.skipTest("Harbor runner dependency is not installed")

        class CapturingEnvironment(FakeEnvironment):
            scoped_exec_env = BaseEnvironment.scoped_exec_env
            _merge_env = BaseEnvironment._merge_env

            def __init__(self) -> None:
                super().__init__()
                self._persistent_env: dict[str, str] = {}
                self._exec_env_overlays = contextvars.ContextVar(
                    "test_opencode_exec_env_overlays",
                    default=(),
                )
                self.executed_envs: list[dict[str, str]] = []

            async def exec(self, command, env=None) -> None:
                del command
                self.executed_envs.append(self._merge_env(env) or {})

            def with_default_user(self, user):
                del user
                return contextlib.nullcontext()

        runtime_secrets = {
            "ANTHROPIC_API_KEY": "fake-runtime-secret",
            "AGENT_FLEET_OPENCODE_SECRET_0123456789ABCDEF": (
                "fake-runtime-secret"
            ),
        }
        with mock.patch.object(
            self.module,
            "OPENCODE_RUNTIME_SECRETS",
            runtime_secrets,
        ):
            agent = self.make_agent("false")
        environment = CapturingEnvironment()
        trial = object.__new__(SingleStepTrial)
        trial.agent = agent
        trial.agent_environment = environment
        trial._emit = mock.AsyncMock()
        trial._network_plan = lambda _step: types.SimpleNamespace(
            agent_env_baseline=None,
            agent_phase=None,
        )

        @contextlib.asynccontextmanager
        async def phase_network_policy(_environment, **_kwargs):
            yield

        trial._phase_network_policy = phase_network_policy
        trial._log_context = lambda *_args, **_kwargs: contextlib.nullcontext()
        target = types.SimpleNamespace()

        asyncio.run(
            trial._run_agent_phase(
                target=target,
                instruction="solve the task",
                timeout_sec=30,
                user=None,
            )
        )

        self.assertTrue(environment.executed_envs)
        for key, value in runtime_secrets.items():
            self.assertEqual(environment.executed_envs[-1][key], value)


class EnableTrackHarborTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_name = "test_enable_track_harbor"
        cls.module = load_module(
            cls.module_name,
            MODULE_DIR / "enable_track_harbor.py",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop(cls.module_name, None)

    def run_main(
        self,
        trace: str,
        environment_type: str = "docker",
        track_disable: str = "",
    ):
        app = mock.Mock()
        patch_e2b_runtime = mock.Mock()
        harbor = types.ModuleType("harbor")
        harbor.__path__ = []
        harbor_cli = types.ModuleType("harbor.cli")
        harbor_cli.__path__ = []
        harbor_main = types.ModuleType("harbor.cli.main")
        harbor_main.app = app
        e2b_runtime = types.ModuleType("e2b_runtime")
        e2b_runtime.patch_e2b_runtime_from_env = patch_e2b_runtime
        modules = {
            "harbor": harbor,
            "harbor.cli": harbor_cli,
            "harbor.cli.main": harbor_main,
            "e2b_runtime": e2b_runtime,
        }

        with (
            mock.patch.dict(
                os.environ,
                {
                    "OPIK_URL": (
                        "" if trace in {"false", "0", ""}
                        else "https://opik.example.invalid/api"
                    ),
                    "OPIK_TRACK_DISABLE": track_disable,
                    "HARBOR_ENVIRONMENT_TYPE": environment_type,
                },
                clear=True,
            ),
            mock.patch.dict(sys.modules, modules),
            mock.patch.object(sys, "argv", ["enable_track_harbor.py", "--help"]),
            mock.patch.object(self.module, "_patch_opik_batch_tags") as patch_batch,
            mock.patch.object(self.module, "_install_track_harbor") as install_tracking,
            mock.patch.object(
                self.module,
                "_patch_trial_decorator_with_harbor_tags",
            ) as patch_tags,
        ):
            self.module.main()

        app.assert_called_once_with()
        return (patch_batch, install_tracking, patch_tags), patch_e2b_runtime

    def test_trace_off_uses_plain_harbor_entrypoint(self) -> None:
        tracking_calls, patch_e2b_runtime = self.run_main("false")
        for call in tracking_calls:
            call.assert_not_called()
        patch_e2b_runtime.assert_not_called()

    def test_trace_on_keeps_host_tracking(self) -> None:
        tracking_calls, patch_e2b_runtime = self.run_main("true")
        for call in tracking_calls:
            call.assert_called_once_with()
        patch_e2b_runtime.assert_not_called()

    def test_track_disable_truthy_values_skip_host_tracking(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                tracking_calls, _ = self.run_main("true", track_disable=value)
                for call in tracking_calls:
                    call.assert_not_called()

    def test_e2b_compatible_backends_apply_runtime_patches(self) -> None:
        for environment_type in ("e2b", "qz"):
            with self.subTest(environment_type=environment_type):
                _, patch_e2b_runtime = self.run_main(
                    "false", environment_type=environment_type
                )
                patch_e2b_runtime.assert_called_once_with()


class FinalizerTraceGateTest(unittest.TestCase):
    """Worker-side timeout replay must stay silent when tracing is off."""

    FINALIZER = MODULE_DIR / "finalize_opencode_sessions.py"

    def run_finalizer(self, env_overrides: dict[str, str]):
        env = os.environ.copy()
        env.pop("OPIK_URL", None)
        env.pop("OPIK_TRACK_DISABLE", None)
        env.update(env_overrides)
        return subprocess.run(
            [
                sys.executable,
                str(self.FINALIZER),
                "--status",
                "timeout",
                "--logs-dir",
                "/nonexistent/trace-gate-probe",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_trace_off_skips_timeout_finalization(self) -> None:
        result = self.run_finalizer({"OPIK_URL": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("finalize skipped", result.stdout)

    def test_opik_track_disable_truthy_values_skip_timeout_finalization(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                result = self.run_finalizer(
                    {
                        "OPIK_URL": "https://opik.example.invalid/api",
                        "OPIK_TRACK_DISABLE": value,
                    }
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("finalize skipped", result.stdout)


if __name__ == "__main__":
    unittest.main()
