"""Tests for the host-configured prebuilt E2B environment."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "e2b_prebuilt.py"


class FakeNetworkMode(str, Enum):
    NO_NETWORK = "no-network"
    PUBLIC = "public"
    ALLOWLIST = "allowlist"


class FakeConnectionConfig:
    envd_port = 49983

    def get_host(self, sandbox_id, sandbox_domain, port):
        return f"{sandbox_id}.{sandbox_domain}:{port}"


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, command: str, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class FakeAsyncSandbox:
    calls: ClassVar[list[dict[str, object]]] = []
    sandbox = SimpleNamespace(commands=FakeCommands())

    @classmethod
    async def create(cls, **kwargs):
        cls.calls.append(kwargs)
        return cls.sandbox


class FakeE2BEnvironment:
    def __init__(self, *args, **kwargs) -> None:
        self._workdir = Path("/app")
        self._sandbox = None
        self.network_policy = SimpleNamespace(network_mode=FakeNetworkMode.PUBLIC)
        self._network_options = None

    async def start(self, force_build: bool) -> None:
        await self._create_sandbox()

    def _sandbox_create_network_options(self):
        return self._network_options


def load_module():
    e2b = types.ModuleType("e2b")
    e2b.AsyncSandbox = FakeAsyncSandbox
    connection_config = types.ModuleType("e2b.connection_config")
    connection_config.ConnectionConfig = FakeConnectionConfig
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    harbor_e2b = types.ModuleType("harbor.environments.e2b")
    harbor_e2b.E2BEnvironment = FakeE2BEnvironment
    harbor_models = types.ModuleType("harbor.models")
    harbor_task = types.ModuleType("harbor.models.task")
    harbor_task_config = types.ModuleType("harbor.models.task.config")
    harbor_task_config.NetworkMode = FakeNetworkMode
    modules = {
        "e2b": e2b,
        "e2b.connection_config": connection_config,
        "harbor": harbor,
        "harbor.environments": environments,
        "harbor.environments.e2b": harbor_e2b,
        "harbor.models": harbor_models,
        "harbor.models.task": harbor_task,
        "harbor.models.task.config": harbor_task_config,
    }
    spec = importlib.util.spec_from_file_location("fleet_e2b_prebuilt", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class PrebuiltE2BEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncSandbox.calls = []
        FakeAsyncSandbox.sandbox = SimpleNamespace(commands=FakeCommands())

    def test_creates_sandbox_from_configured_template_and_prepares_workdir(self) -> None:
        module = load_module()
        with patch.dict(
            os.environ,
            {
                "HARBOR_E2B_PREBUILT_TEMPLATE": "template-test",
                "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "900",
            },
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            asyncio.run(environment.start(False))

        self.assertEqual(
            FakeAsyncSandbox.calls,
            [
                {
                    "template": "template-test",
                    "timeout": 900,
                    "allow_internet_access": True,
                    "network": None,
                }
            ],
        )
        command, kwargs = FakeAsyncSandbox.sandbox.commands.calls[0]
        self.assertEqual(command, "mkdir -p -- /app && chmod 0777 -- /app")
        self.assertEqual(kwargs["user"], "root")
        self.assertEqual(kwargs["cwd"], "/")

    def test_disables_internet_for_no_network_policy(self) -> None:
        module = load_module()
        with patch.dict(
            os.environ,
            {"HARBOR_E2B_PREBUILT_TEMPLATE": "template-test"},
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            environment.network_policy = SimpleNamespace(
                network_mode=FakeNetworkMode.NO_NETWORK
            )
            asyncio.run(environment.start(False))

        self.assertFalse(FakeAsyncSandbox.calls[0]["allow_internet_access"])
        self.assertIsNone(FakeAsyncSandbox.calls[0]["network"])

    def test_forwards_allowlist_network_options(self) -> None:
        module = load_module()
        network_options = {
            "allow_out": ["api.example.com"],
            "deny_out": ["0.0.0.0/0"],
        }
        with patch.dict(
            os.environ,
            {"HARBOR_E2B_PREBUILT_TEMPLATE": "template-test"},
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            environment.network_policy = SimpleNamespace(
                network_mode=FakeNetworkMode.ALLOWLIST
            )
            environment._network_options = network_options
            asyncio.run(environment.start(False))

        self.assertTrue(FakeAsyncSandbox.calls[0]["allow_internet_access"])
        self.assertIs(FakeAsyncSandbox.calls[0]["network"], network_options)

    def test_default_timeout_matches_harbor_e2b_lifetime(self) -> None:
        module = load_module()
        with patch.dict(
            os.environ,
            {"HARBOR_E2B_PREBUILT_TEMPLATE": "template-test"},
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            asyncio.run(environment.start(False))

        self.assertEqual(FakeAsyncSandbox.calls[0]["timeout"], 86_400)

    def test_force_build_is_rejected(self) -> None:
        module = load_module()
        with patch.dict(
            os.environ,
            {"HARBOR_E2B_PREBUILT_TEMPLATE": "template-test"},
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            with self.assertRaisesRegex(RuntimeError, "force_build"):
                asyncio.run(environment.start(True))

        self.assertEqual(FakeAsyncSandbox.calls, [])

    def test_template_must_come_from_host_environment(self) -> None:
        module = load_module()
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "HARBOR_E2B_PREBUILT_TEMPLATE"),
        ):
            module.PrebuiltE2BEnvironment()


if __name__ == "__main__":
    unittest.main()
