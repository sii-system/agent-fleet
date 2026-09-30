from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = HARBOR_DIR / "qz_e2b_sandbox.py"
sys.path.insert(0, str(HARBOR_DIR))

import qz_template_manager as template_manager
import qz_template_mapping as template_mapping


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    capabilities = types.ModuleType("harbor.environments.capabilities")
    e2b = types.ModuleType("harbor.environments.e2b")
    models = types.ModuleType("harbor.models")
    task = types.ModuleType("harbor.models.task")
    task_config = types.ModuleType("harbor.models.task.config")

    class Capability:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    capabilities.EnvironmentCapabilities = Capability
    capabilities.EnvironmentResourceCapabilities = Capability

    class E2BEnvironment:
        init_calls: ClassVar[list[tuple[tuple, dict]]] = []
        start_calls: ClassVar[list[bool]] = []
        exist_calls: ClassVar[list[str]] = []

        def __init__(self, *args, **kwargs):
            type(self).init_calls.append((args, kwargs))
            self._template_name = "hello-world__abc.123"
            self.environment_name = "test-environment"
            self.session_id = "test-session"
            self.network_policy = types.SimpleNamespace(network_mode="allow")
            self._workdir = Path("/dockerfile-workdir")
            self.task_env_config = types.SimpleNamespace(
                workdir="/task-config-workdir"
            )

        async def start(self, force_build: bool) -> None:
            type(self).start_calls.append(force_build)

        async def _does_template_exist(self) -> bool:
            type(self).exist_calls.append(self._template_name)
            return False

        def _sandbox_create_network_options(self):
            return None

    e2b.E2BEnvironment = E2BEnvironment

    class NetworkMode:
        NO_NETWORK = "none"

    task_config.NetworkMode = NetworkMode
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": environments,
            "harbor.environments.capabilities": capabilities,
            "harbor.environments.e2b": e2b,
            "harbor.models": models,
            "harbor.models.task": task,
            "harbor.models.task.config": task_config,
        }
    )


def load_module():
    install_harbor_stubs()
    spec = importlib.util.spec_from_file_location("qz_e2b_sandbox", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def retry_dependency_stubs():
    """Provide enough of e2b/httpx/httpcore/tenacity to exercise retries."""

    httpcore = types.ModuleType("httpcore")
    httpx = types.ModuleType("httpx")
    e2b_pkg = types.ModuleType("e2b")
    e2b_pkg.__path__ = []
    e2b_exceptions = types.ModuleType("e2b.exceptions")
    tenacity = types.ModuleType("tenacity")

    class HttpcoreConnectError(Exception):
        pass

    class HttpcoreConnectTimeout(Exception):
        pass

    class HttpcorePoolTimeout(Exception):
        pass

    class HttpcoreReadTimeout(Exception):
        pass

    class HttpxConnectError(Exception):
        pass

    class HttpxConnectTimeout(Exception):
        pass

    class HttpxPoolTimeout(Exception):
        pass

    class HttpxReadTimeout(Exception):
        pass

    class RateLimitException(Exception):
        pass

    httpcore.ConnectError = HttpcoreConnectError
    httpcore.ConnectTimeout = HttpcoreConnectTimeout
    httpcore.PoolTimeout = HttpcorePoolTimeout
    httpcore.ReadTimeout = HttpcoreReadTimeout
    httpx.ConnectError = HttpxConnectError
    httpx.ConnectTimeout = HttpxConnectTimeout
    httpx.PoolTimeout = HttpxPoolTimeout
    httpx.ReadTimeout = HttpxReadTimeout
    e2b_exceptions.RateLimitException = RateLimitException

    def retry_if_exception_type(exception_types):
        return exception_types

    def stop_after_attempt(attempts):
        return attempts

    def wait_exponential(**_kwargs):
        return None

    def retry(*, retry, stop, wait, reraise):
        del wait, reraise

        def decorate(fn):
            async def wrapped(*args, **kwargs):
                for attempt in range(stop):
                    try:
                        return await fn(*args, **kwargs)
                    except retry:
                        if attempt + 1 >= stop:
                            raise
                raise AssertionError("retry loop exhausted without returning")

            return wrapped

        return decorate

    tenacity.retry = retry
    tenacity.retry_if_exception_type = retry_if_exception_type
    tenacity.stop_after_attempt = stop_after_attempt
    tenacity.wait_exponential = wait_exponential

    return (
        {
            "httpcore": httpcore,
            "httpx": httpx,
            "e2b": e2b_pkg,
            "e2b.exceptions": e2b_exceptions,
            "tenacity": tenacity,
        },
        {
            "connect": HttpcoreConnectError,
            "rate_limit": RateLimitException,
            "read_timeout": HttpxReadTimeout,
            "generic": ValueError,
        },
    )


QZ_VARS = (
    "QZ_CREATE_CONCURRENCY",
    "QZ_SANDBOX_API_KEY",
    "QZ_SANDBOX_API_URL",
    "SBX_API_KEY",
    "SBX_API_URL",
    "E2B_API_KEY",
    "E2B_API_URL",
    "E2B_VALIDATE_API_KEY",
    "E2B_SANDBOX_URL",
    "E2B_DOMAIN",
    "E2B_DEBUG",
    "E2B_ACCESS_TOKEN",
)


class ApplyQzEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def run_mapping(self, env: dict[str, str]) -> dict[str, str]:
        cleaned = {name: "" for name in QZ_VARS}
        cleaned.update(env)
        with patch.dict(os.environ, cleaned, clear=False):
            for name in QZ_VARS:
                if not cleaned.get(name):
                    os.environ.pop(name, None)
            self.module.apply_qz_environment()
            return {name: os.environ.get(name, "") for name in QZ_VARS}

    def test_sbx_values_map_to_e2b_with_v1_suffix(self):
        result = self.run_mapping(
            {
                "SBX_API_KEY": "sbx_secret",
                "SBX_API_URL": "https://qz-sbx-api.sii.edu.cn",
            }
        )
        self.assertEqual(result["E2B_API_KEY"], "sbx_secret")
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")
        self.assertEqual(result["E2B_VALIDATE_API_KEY"], "false")

    def test_defaults_apply_without_any_url(self):
        result = self.run_mapping({"SBX_API_KEY": "sbx_secret"})
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")

    def test_url_with_existing_v1_suffix_is_not_doubled(self):
        result = self.run_mapping(
            {
                "SBX_API_KEY": "sbx_secret",
                "SBX_API_URL": "https://qz-sbx-api.sii.edu.cn/v1/",
            }
        )
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")

    def test_qz_specific_variables_win_over_sbx(self):
        result = self.run_mapping(
            {
                "QZ_SANDBOX_API_KEY": "sbx_qz",
                "SBX_API_KEY": "sbx_other",
                "QZ_SANDBOX_API_URL": "https://alt.example.com",
                "SBX_API_URL": "https://qz-sbx-api.sii.edu.cn",
            }
        )
        self.assertEqual(result["E2B_API_KEY"], "sbx_qz")
        self.assertEqual(result["E2B_API_URL"], "https://alt.example.com/v1")

    def test_qz_values_override_and_clear_ambient_cloud_e2b_settings(self):
        # On a mixed rollout host the ambient E2B_* variables belong to the
        # cloud-E2B backend; a qz process must not send its requests there.
        result = self.run_mapping(
            {
                "SBX_API_KEY": "sbx_secret",
                "SBX_API_URL": "https://qz-sbx-api.sii.edu.cn",
                "E2B_API_KEY": "e2b_cloud_key",
                "E2B_API_URL": "https://api.e2b.app",
                "E2B_VALIDATE_API_KEY": "true",
                "E2B_SANDBOX_URL": "https://sandbox.e2b.app",
                "E2B_DOMAIN": "e2b.app",
                "E2B_DEBUG": "true",
                "E2B_ACCESS_TOKEN": "cloud_access_token",
            }
        )
        self.assertEqual(result["E2B_API_KEY"], "sbx_secret")
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")
        self.assertEqual(result["E2B_VALIDATE_API_KEY"], "false")
        self.assertEqual(result["E2B_SANDBOX_URL"], "")
        self.assertEqual(result["E2B_DOMAIN"], "")
        self.assertEqual(result["E2B_DEBUG"], "")
        self.assertEqual(result["E2B_ACCESS_TOKEN"], "")

    def test_sbx_prefixed_e2b_key_serves_as_legacy_fallback(self):
        result = self.run_mapping({"E2B_API_KEY": "sbx_legacy"})
        self.assertEqual(result["E2B_API_KEY"], "sbx_legacy")
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")

    def test_ambient_cloud_e2b_key_is_cleared_without_qz_key(self):
        result = self.run_mapping(
            {
                "E2B_API_KEY": "e2b_cloud_key",
                "E2B_API_URL": "https://api.e2b.app",
            }
        )
        self.assertEqual(result["E2B_API_KEY"], "")
        self.assertEqual(result["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1")

    def test_empty_exported_placeholders_count_as_unset(self):
        # env.sh exports empty placeholders so worker panes inherit the names.
        cleaned = {name: "" for name in QZ_VARS}
        cleaned["SBX_API_KEY"] = "sbx_secret"
        with patch.dict(os.environ, cleaned, clear=False):
            self.module.apply_qz_environment()
            self.assertEqual(os.environ["E2B_API_KEY"], "sbx_secret")
            self.assertEqual(
                os.environ["E2B_API_URL"], "https://qz-sbx-api.sii.edu.cn/v1"
            )
            self.assertEqual(os.environ["E2B_VALIDATE_API_KEY"], "false")


def install_e2b_stub():
    e2b_pkg = types.ModuleType("e2b")
    cc_mod = types.ModuleType("e2b.connection_config")

    class ConnectionConfig:
        domain = "fallback.example.com"

        def get_host(self, sandbox_id, sandbox_domain, port):
            return f"{port}-{sandbox_id}.{sandbox_domain}"

    cc_mod.ConnectionConfig = ConnectionConfig
    sys.modules["e2b"] = e2b_pkg
    sys.modules["e2b.connection_config"] = cc_mod
    return ConnectionConfig


class PatchEnvdHostTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.cc_cls = install_e2b_stub()

    def host(self):
        return self.cc_cls().get_host("sbx123", "openapi-qb-nat.sii.edu.cn", 49983)

    def test_patch_adds_sbx_prefix(self):
        self.module.patch_envd_host()
        self.assertEqual(self.host(), "sbx-49983-sbx123.openapi-qb-nat.sii.edu.cn")

    def test_prefix_override_via_environment(self):
        self.module.patch_envd_host()
        with patch.dict(os.environ, {"QZ_SANDBOX_HOST_PREFIX": "alt-"}, clear=False):
            self.assertEqual(self.host(), "alt-49983-sbx123.openapi-qb-nat.sii.edu.cn")

    def test_patch_is_idempotent(self):
        self.module.patch_envd_host()
        first = self.cc_cls.get_host
        self.module.patch_envd_host()
        self.assertIs(self.cc_cls.get_host, first)

    def test_empty_sandbox_domain_falls_back_to_config_domain(self):
        self.module.patch_envd_host()
        result = self.cc_cls().get_host("sbx123", "", 49983)
        self.assertEqual(result, "sbx-49983-sbx123.fallback.example.com")

    def test_missing_e2b_extra_is_a_noop(self):
        sys.modules.pop("e2b.connection_config", None)
        sys.modules.pop("e2b", None)
        self.module.patch_envd_host()  # must not raise


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_preflight_without_key_exits(self):
        cleaned = {name: "" for name in QZ_VARS}
        with patch.dict(os.environ, cleaned, clear=False):
            for name in QZ_VARS:
                os.environ.pop(name, None)
            with self.assertRaises(SystemExit):
                self.module.QzSandboxEnvironment.preflight()

    def test_preflight_with_sbx_key_passes(self):
        cleaned = {name: "" for name in QZ_VARS}
        cleaned["SBX_API_KEY"] = "sbx_secret"
        with patch.dict(os.environ, cleaned, clear=False):
            self.module.QzSandboxEnvironment.preflight()
            self.assertEqual(os.environ["E2B_API_KEY"], "sbx_secret")

    def test_preflight_rejects_ambient_cloud_e2b_key(self):
        cleaned = {name: "" for name in QZ_VARS}
        cleaned.update(
            {
                "E2B_API_KEY": "e2b_cloud_key",
                "E2B_API_URL": "https://api.e2b.app",
            }
        )
        with patch.dict(os.environ, cleaned, clear=False):
            for name in QZ_VARS:
                if not cleaned.get(name):
                    os.environ.pop(name, None)
            with self.assertRaises(SystemExit):
                self.module.QzSandboxEnvironment.preflight()
            self.assertNotIn("E2B_API_KEY", os.environ)

    def test_preflight_rejects_invalid_create_concurrency(self):
        cleaned = {name: "" for name in QZ_VARS}
        cleaned.update(
            {
                "SBX_API_KEY": "sbx_secret",
                "QZ_CREATE_CONCURRENCY": "0",
            }
        )
        with (
            patch.dict(os.environ, cleaned, clear=False),
            self.assertRaisesRegex(SystemExit, "QZ_CREATE_CONCURRENCY"),
        ):
            self.module.QzSandboxEnvironment.preflight()

    def test_init_applies_mapping_before_super(self):
        cleaned = {name: "" for name in QZ_VARS}
        cleaned["SBX_API_KEY"] = "sbx_secret"
        with patch.dict(os.environ, cleaned, clear=False):
            for name in QZ_VARS:
                if not cleaned.get(name):
                    os.environ.pop(name, None)
            self.module.QzSandboxEnvironment()
            self.assertEqual(os.environ["E2B_API_KEY"], "sbx_secret")


class CreateRetryTest(unittest.TestCase):
    def run_create(
        self,
        failure_name: str | None,
        *,
        succeeds_after_failure: bool,
        prepare_failure_name: str | None = None,
        prepare_exit_code: int = 0,
        init_steps: tuple[dict[str, str], ...] = (),
        init_exit_code: int = 0,
    ) -> tuple[int, Exception | None, list[tuple[str, dict]]]:
        dependency_modules, error_types = retry_dependency_stubs()
        expected_error_types = (*error_types.values(), RuntimeError)
        command_calls: list[tuple[str, dict]] = []

        class FakeCommands:
            async def run(self, command, **kwargs):
                command_calls.append((command, kwargs))
                if prepare_failure_name is not None:
                    raise error_types[prepare_failure_name]("prepare failed")
                exit_code = (
                    init_exit_code if len(command_calls) > 1 else prepare_exit_code
                )
                return types.SimpleNamespace(
                    exit_code=exit_code,
                    stderr="command stderr" if exit_code else "",
                    stdout="",
                )

        sandbox = types.SimpleNamespace(commands=FakeCommands())
        outcomes: list[object] = []
        if failure_name is not None:
            outcomes.append(error_types[failure_name]("boom"))
        if succeeds_after_failure:
            outcomes.append(sandbox)
        calls: list[dict] = []

        class FakeAsyncSandbox:
            @classmethod
            async def create(cls, **kwargs):
                calls.append(kwargs)
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        dependency_modules["e2b"].AsyncSandbox = FakeAsyncSandbox
        cleaned = {name: "" for name in QZ_VARS}
        cleaned["SBX_API_KEY"] = "sbx_secret"
        error = None
        with (
            patch.dict(sys.modules, dependency_modules),
            patch.dict(os.environ, cleaned, clear=False),
        ):
            for name in QZ_VARS:
                if not cleaned.get(name):
                    os.environ.pop(name, None)
            module = load_module()
            environment = module.QzSandboxEnvironment(template="test_template")
            environment._workdir = Path("/app")
            environment._task_init_steps = init_steps
            try:
                asyncio.run(environment._create_sandbox())
            except expected_error_types as exc:
                error = exc
        return len(calls), error, command_calls

    def test_connection_failure_is_retried_once(self):
        calls, error, _ = self.run_create("connect", succeeds_after_failure=True)
        self.assertEqual(calls, 2)
        self.assertIsNone(error)

    def test_rate_limit_is_retried_once(self):
        calls, error, _ = self.run_create("rate_limit", succeeds_after_failure=True)
        self.assertEqual(calls, 2)
        self.assertIsNone(error)

    def test_post_dispatch_read_timeout_is_not_retried(self):
        calls, error, _ = self.run_create("read_timeout", succeeds_after_failure=False)
        self.assertEqual(calls, 1)
        self.assertIsNotNone(error)
        self.assertEqual(type(error).__name__, "HttpxReadTimeout")

    def test_generic_failure_is_not_retried(self):
        calls, error, _ = self.run_create("generic", succeeds_after_failure=False)
        self.assertEqual(calls, 1)
        self.assertIsInstance(error, ValueError)

    def test_prepares_workdir_after_allocation(self):
        calls, error, command_calls = self.run_create(None, succeeds_after_failure=True)

        self.assertEqual(calls, 1)
        self.assertIsNone(error)
        self.assertEqual(
            command_calls,
            [
                (
                    "mkdir -p -- /app && chmod 0777 -- /app",
                    {"user": "root", "cwd": "/", "timeout": 30},
                )
            ],
        )

    def test_workdir_transport_failure_does_not_retry_allocation(self):
        calls, error, command_calls = self.run_create(
            None,
            succeeds_after_failure=True,
            prepare_failure_name="connect",
        )

        self.assertEqual(calls, 1)
        self.assertEqual(len(command_calls), 1)
        self.assertEqual(type(error).__name__, "HttpcoreConnectError")

    def test_workdir_command_failure_is_actionable(self):
        calls, error, _ = self.run_create(
            None,
            succeeds_after_failure=True,
            prepare_exit_code=23,
        )

        self.assertEqual(calls, 1)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("failed to prepare task workdir /app", str(error))
        self.assertIn("exit 23: command stderr", str(error))

    def test_runs_task_init_after_workdir_preparation(self):
        calls, error, command_calls = self.run_create(
            None,
            succeeds_after_failure=True,
            init_steps=(
                {"run": "git fetch && git checkout instance-a", "cwd": "/testbed"},
            ),
        )

        self.assertEqual(calls, 1)
        self.assertIsNone(error)
        self.assertEqual(
            command_calls[1],
            (
                "git fetch && git checkout instance-a",
                {"cwd": "/testbed", "timeout": 600},
            ),
        )

    def test_task_init_failure_does_not_retry_allocation(self):
        calls, error, command_calls = self.run_create(
            None,
            succeeds_after_failure=True,
            init_steps=({"run": "git checkout missing", "cwd": "/testbed"},),
            init_exit_code=7,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(len(command_calls), 2)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("QZ task init step 1 failed", str(error))
        self.assertIn("exit 7: command stderr", str(error))


class TemplateResolutionTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.env_vars = dict.fromkeys(
            (
                *QZ_VARS,
                "QZ_SANDBOX_TEMPLATE",
                "QZ_SANDBOX_TEMPLATE_MAP",
            ),
            "",
        )
        self.env_vars["SBX_API_KEY"] = "sbx_secret"

    def make_env(self, **kwargs):
        with patch.dict(os.environ, self.env_vars, clear=False):
            for name, value in self.env_vars.items():
                if not value:
                    os.environ.pop(name, None)
            return self.module.QzSandboxEnvironment(**kwargs)

    def test_sanitize_template_name(self):
        self.assertEqual(
            self.module.sanitize_template_name("hello-world__abc.123/x"),
            "hello_world__abc_123_x",
        )

    def test_auto_template_name_is_sanitized(self):
        env = self.make_env()
        self.assertEqual(env._template_name, "hello_world__abc_123")

    def test_template_kwarg_wins(self):
        env = self.make_env(template="agent_fleet_probe")
        self.assertEqual(env._template_name, "agent_fleet_probe")

    def test_template_env_override(self):
        self.env_vars["QZ_SANDBOX_TEMPLATE"] = "agent_fleet_probe"
        env = self.make_env()
        self.assertEqual(env._template_name, "agent_fleet_probe")

    def test_template_mapping_resolves_environment_name(self):
        self.env_vars["QZ_SANDBOX_TEMPLATE_MAP"] = "/tmp/qz-map.json"
        with patch.object(
            self.module,
            "resolve_task_environment_from_environment",
            return_value=types.SimpleNamespace(
                template_id="mapped-template-id",
                init_steps=({"run": "git checkout task", "cwd": "/testbed"},),
                workdir="/root/repository",
            ),
        ) as resolve:
            env = self.make_env()

        self.assertEqual(env._template_name, "mapped-template-id")
        self.assertEqual(env._template_override, "mapped-template-id")
        self.assertEqual(
            env._task_init_steps,
            ({"run": "git checkout task", "cwd": "/testbed"},),
        )
        self.assertEqual(env._workdir, Path("/root/repository"))
        self.assertEqual(env.task_env_config.workdir, "/root/repository")
        resolve.assert_called_once_with(
            Path("/tmp/qz-map.json"),
            "test-environment",
        )

    def test_template_mapping_uses_real_resolver_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir = root / "test-environment"
            task_dir.mkdir()
            (task_dir / "task.toml").write_text(
                '[environment]\ndocker_image = "ubuntu:24.04"\n',
                encoding="utf-8",
            )
            payload = template_mapping.build_inventory(
                benchmark="test",
                tasks=[("test-environment", task_dir)],
            )
            entry = next(iter(payload["templates"].values()))
            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps(payload), encoding="utf-8")
            self.env_vars["QZ_SANDBOX_TEMPLATE_MAP"] = str(mapping_path)
            client = Mock()
            client.get_by_name.return_value = {
                "templateID": "mapped-template-id",
                "names": [entry["template_name"]],
                "builds": [
                    {
                        "createdAt": "2026-08-19T00:00:00Z",
                        "sbxSpecCode": entry["spec"],
                        "status": "ready",
                    }
                ],
            }
            with patch.object(
                template_manager,
                "client_from_environment",
                return_value=client,
            ):
                env = self.make_env()

        self.assertEqual(env._template_name, "mapped-template-id")
        client.get_by_name.assert_called_once_with(entry["template_name"])

    def test_fixed_template_and_mapping_are_mutually_exclusive(self):
        self.env_vars["QZ_SANDBOX_TEMPLATE"] = "fixed-template"
        self.env_vars["QZ_SANDBOX_TEMPLATE_MAP"] = "/tmp/qz-map.json"
        with self.assertRaisesRegex(ValueError, "set only one"):
            self.make_env()

    def test_start_rejects_force_build(self):
        env = self.make_env()
        with self.assertRaises(RuntimeError):
            asyncio.run(env.start(True))

    def test_start_passes_through_without_force_build(self):
        env = self.make_env()
        env.start_calls.clear()
        asyncio.run(env.start(False))
        self.assertEqual(env.start_calls, [False])

    def test_capabilities_advertise_only_verified_behavior(self):
        env = self.make_env()
        caps = env.capabilities
        self.assertFalse(caps.__dict__.get("disable_internet"))
        self.assertFalse(caps.__dict__.get("gpus"))
        resource = self.module.QzSandboxEnvironment.resource_capabilities()
        self.assertEqual(resource.__dict__, {})

    def test_create_template_raises(self):
        env = self.make_env()
        with self.assertRaises(RuntimeError):
            asyncio.run(env._create_template())

    def test_timeout_default_and_valid_values(self):
        with patch.dict(os.environ, {"QZ_SANDBOX_TIMEOUT_SEC": ""}, clear=False):
            self.assertEqual(self.module.qz_sandbox_timeout_sec(), 14400)
        with patch.dict(os.environ, {"QZ_SANDBOX_TIMEOUT_SEC": "600"}, clear=False):
            self.assertEqual(self.module.qz_sandbox_timeout_sec(), 600)

    def test_timeout_rejects_bad_values(self):
        for bad in ("abc", "0", "-5", "14401"):
            with (
                patch.dict(os.environ, {"QZ_SANDBOX_TIMEOUT_SEC": bad}, clear=False),
                self.assertRaises(ValueError),
            ):
                self.module.qz_sandbox_timeout_sec()

    def test_preflight_rejects_bad_timeout(self):
        self.env_vars["QZ_SANDBOX_TIMEOUT_SEC"] = "not-a-number"
        with (
            patch.dict(os.environ, self.env_vars, clear=False),
            self.assertRaises(SystemExit),
        ):
            self.module.QzSandboxEnvironment.preflight()

    def test_override_skips_alias_precheck(self):
        # An override may be a template ID, invisible to the alias lookup;
        # creation is the authority, so the pre-check must pass it through.
        env = self.make_env(template="erbkewn6i1y4zf41mpz8")
        env.exist_calls.clear()
        self.assertTrue(asyncio.run(env._does_template_exist()))
        self.assertEqual(env.exist_calls, [])

    def test_auto_alias_still_prechecked(self):
        env = self.make_env()
        env.exist_calls.clear()
        self.assertFalse(asyncio.run(env._does_template_exist()))
        self.assertEqual(env.exist_calls, ["hello_world__abc_123"])


if __name__ == "__main__":
    unittest.main()
