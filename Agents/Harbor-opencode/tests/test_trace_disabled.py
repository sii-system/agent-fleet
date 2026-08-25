from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
        if (
            not self.fake_opencode_present
            and command.startswith('export PATH="$HOME/.local/bin:$PATH"; ')
            and "opencode --version >/dev/null" in command
        ):
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

    def _local_install_command(self) -> str:
        agent = self.make_agent("false", opencode_present=False)

        async def run_once(_label, operation, **_kwargs) -> None:
            await operation()

        with mock.patch.object(self.module, "_retry_async", run_once):
            asyncio.run(agent.install(FakeEnvironment()))
        return next(
            str(item.get("command", ""))
            for item in agent.agent_commands
            if "opencode_version=" in str(item.get("command", ""))
        )

    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def _run_node_runtime_probe(
        self,
        *,
        system_node: str,
        system_npm: str,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        install_command = self._local_install_command()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            local_bin = home / ".local" / "bin"
            tools_bin = root / "tools"
            cache_dir = root / "cache"
            runtime_bin = root / "node-v22.14.0-linux-x64" / "bin"
            cache_log = root / "cache-runtime.log"
            local_bin.mkdir(parents=True)
            tools_bin.mkdir()
            cache_dir.mkdir()
            runtime_bin.mkdir(parents=True)

            for command in (
                "dirname",
                "find",
                "ln",
                "mkdir",
                "mktemp",
                "tar",
                "uname",
            ):
                source = shutil.which(command)
                self.assertIsNotNone(source, command)
                (tools_bin / command).symlink_to(source)

            if system_node == "healthy":
                self._write_executable(
                    tools_bin / "node",
                    'test "${1:-}" = "--version" && echo v20.0.0\nexit 0',
                )
            elif system_node == "broken":
                self._write_executable(tools_bin / "node", "exit 1")
            elif system_node != "missing":
                self.fail(f"unknown system_node state: {system_node}")

            if system_npm == "healthy":
                self._write_executable(
                    tools_bin / "npm",
                    'test "${1:-}" = "--version" && echo 10.0.0\nexit 0',
                )
            elif system_npm == "broken":
                self._write_executable(tools_bin / "npm", "exit 1")
            elif system_npm != "missing":
                self.fail(f"unknown system_npm state: {system_npm}")

            cache_log_q = shlex.quote(str(cache_log))
            self._write_executable(
                runtime_bin / "node",
                (
                    f"printf 'node:%s\\n' \"$*\" >> {cache_log_q}\n"
                    'test "${1:-}" = "--version" && echo v22.14.0\n'
                    "exit 0"
                ),
            )
            self._write_executable(
                runtime_bin / "npm",
                (
                    f"printf 'npm:%s\\n' \"$*\" >> {cache_log_q}\n"
                    'test "${1:-}" = "--version" && echo 10.9.2\n'
                    "exit 0"
                ),
            )
            self._write_executable(runtime_bin / "npx", "exit 0")
            with tarfile.open(cache_dir / "node-runtime.tar.xz", "w") as archive:
                archive.add(runtime_bin.parent, arcname=runtime_bin.parent.name)

            self._write_executable(
                local_bin / "opencode",
                'test "${1:-}" = "--version" && echo 1.0.0\nexit 0',
            )
            result = subprocess.run(
                ["/bin/bash", "-c", install_command],
                check=False,
                capture_output=True,
                env={
                    "CC_NODE_DIST_URL": "",
                    "CC_OPIK_PY_WHEEL_DIR": str(cache_dir),
                    "HARBOR_LOCAL_OPENCODE_LINUX_X64_TGZ_URL": "",
                    "HARBOR_LOCAL_OPENCODE_TGZ_URL": "",
                    "HARBOR_LOCAL_WHEEL_SERVER_URL": "",
                    "HOME": str(home),
                    "OPENCODE_LINUX_X64_TGZ_PATH": "",
                    "OPENCODE_TGZ_PATH": "",
                    "PATH": str(tools_bin),
                },
                text=True,
            )
            cache_events = (
                cache_log.read_text(encoding="utf-8").splitlines()
                if cache_log.exists()
                else []
            )
            return result, cache_events

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

    def test_install_preflight_enforces_node_18_and_npm(self) -> None:
        agent = self.make_agent("false")

        asyncio.run(agent.install(FakeEnvironment()))

        preflight = next(
            str(item.get("command", ""))
            for item in agent.agent_commands
            if "opencode --version >/dev/null" in str(item.get("command", ""))
        )
        self.assertIn(self.module.NODE_RUNTIME_READY_COMMAND, preflight)
        self.assertIn(">= 18 ? 0 : 1", preflight)
        self.assertIn("npm --version", preflight)

    def test_install_uses_sandbox_reachable_node_dist_before_apt(self) -> None:
        install_command = self._local_install_command()
        self.assertIn("${CC_NODE_DIST_URL:-}", install_command)
        self.assertIn("node_runtime_ready()", install_command)
        self.assertIn(">= 18 ? 0 : 1", install_command)
        self.assertIn(
            'if download_file "$CC_NODE_DIST_URL" "$node_dist_tgz" '
            '    && [ -s "$node_dist_tgz" ]; then',
            install_command,
        )
        self.assertIn(
            'if extract_archive "$node_dist_tgz" "$node_dir"; then',
            install_command,
        )
        self.assertLess(
            install_command.index("CC_NODE_DIST_URL"),
            install_command.index("apt-get update"),
        )
        self.assertIn('npm install -g "opencode-ai@${opencode_version}"', install_command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=install_command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_install_uses_cached_node_when_npm_is_broken_and_node_missing(self) -> None:
        result, cache_events = self._run_node_runtime_probe(
            system_node="missing",
            system_npm="broken",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(event.startswith("node:-e ") for event in cache_events)
        )
        self.assertIn("npm:--version", cache_events)

    def test_install_uses_cached_node_when_existing_node_is_broken(self) -> None:
        result, cache_events = self._run_node_runtime_probe(
            system_node="broken",
            system_npm="healthy",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(event.startswith("node:-e ") for event in cache_events)
        )
        self.assertIn("npm:--version", cache_events)

    def test_install_keeps_healthy_node_runtime(self) -> None:
        result, cache_events = self._run_node_runtime_probe(
            system_node="healthy",
            system_npm="healthy",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cache_events, [])

    def test_install_rechecks_cached_node_runtime_after_hash_refresh(self) -> None:
        install_command = self._local_install_command()

        self.assertIn(
            f"node_runtime_ready() {{   "
            f"{self.module.NODE_RUNTIME_READY_COMMAND}; }};",
            install_command,
        )
        self.assertNotIn("if ! command -v npm", install_command)
        self.assertIn(
            "hash -r;   if node_runtime_ready; then return 0; fi;",
            install_command,
        )
        self.assertEqual(
            install_command.count(
                'activate_node_runtime "$node_runtime_bin" || true;'
            ),
            2,
        )
        self.assertIn(
            'activate_node_runtime "$system_node_runtime_bin"',
            install_command,
        )
        self.assertIn(
            'rm -f "$HOME/.local/bin/node" "$HOME/.local/bin/npm"',
            install_command,
        )
        self.assertGreaterEqual(install_command.count("hash -r;"), 3)
        self.assertIn(
            "if ! node_runtime_ready; then   echo "
            "'[ERROR] Node.js/npm runtime is unavailable or unhealthy'",
            install_command,
        )

    def test_install_requires_python_39_for_verifier_runtime(self) -> None:
        agent = self.make_agent("false")

        asyncio.run(agent.install(FakeEnvironment()))

        prepare_command = str(agent.root_commands[0].get("command", ""))
        self.assertIn("sys.version_info >= (3, 9)", prepare_command)
        self.assertIn("python3.12-runtime.tar.gz", prepare_command)

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
        self.assertNotIn("timeout_sec", agent.agent_commands[-1])

    def test_run_forwards_explicit_positive_timeout(self) -> None:
        agent = self.make_agent("false")

        with mock.patch.dict(
            os.environ,
            {"HARBOR_OPENCODE_RUN_TIMEOUT_SEC": "2400"},
        ):
            asyncio.run(agent.run("solve the task", FakeEnvironment(), object()))

        self.assertEqual(agent.agent_commands[-1].get("timeout_sec"), 2400)

    def test_run_rejects_non_integer_timeout(self) -> None:
        agent = self.make_agent("false")

        with (
            mock.patch.dict(
                os.environ,
                {"HARBOR_OPENCODE_RUN_TIMEOUT_SEC": "30m"},
            ),
            self.assertRaisesRegex(
                ValueError,
                "HARBOR_OPENCODE_RUN_TIMEOUT_SEC must be a positive integer",
            ),
        ):
            asyncio.run(agent.run("solve the task", FakeEnvironment(), object()))

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
