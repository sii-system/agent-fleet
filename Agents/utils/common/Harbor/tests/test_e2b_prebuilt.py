#!/usr/bin/env python3
"""Tests for the host-configured prebuilt E2B environment."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "e2b_prebuilt.py"


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
    calls: list[dict[str, object]] = []
    sandbox = SimpleNamespace(commands=FakeCommands())

    @classmethod
    async def create(cls, **kwargs):
        cls.calls.append(kwargs)
        return cls.sandbox


class FakeE2BEnvironment:
    def __init__(self, *args, **kwargs) -> None:
        self._workdir = Path("/app")
        self._sandbox = None

    async def start(self, force_build: bool) -> None:
        await self._create_sandbox()


def load_module():
    e2b = types.ModuleType("e2b")
    e2b.AsyncSandbox = FakeAsyncSandbox
    connection_config = types.ModuleType("e2b.connection_config")
    connection_config.ConnectionConfig = FakeConnectionConfig
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    harbor_e2b = types.ModuleType("harbor.environments.e2b")
    harbor_e2b.E2BEnvironment = FakeE2BEnvironment
    modules = {
        "e2b": e2b,
        "e2b.connection_config": connection_config,
        "harbor": harbor,
        "harbor.environments": environments,
        "harbor.environments.e2b": harbor_e2b,
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
                "TB_E2B_PREBUILT_TEMPLATE": "template-test",
                "TB_E2B_SANDBOX_TIMEOUT_SEC": "900",
            },
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            asyncio.run(environment.start(False))

        self.assertEqual(
            FakeAsyncSandbox.calls,
            [{"template": "template-test", "timeout": 900}],
        )
        command, kwargs = FakeAsyncSandbox.sandbox.commands.calls[0]
        self.assertEqual(command, "mkdir -p -- /app && chmod 0777 -- /app")
        self.assertEqual(kwargs["user"], "root")
        self.assertEqual(kwargs["cwd"], "/")

    def test_force_build_is_rejected(self) -> None:
        module = load_module()
        with patch.dict(
            os.environ,
            {"TB_E2B_PREBUILT_TEMPLATE": "template-test"},
            clear=True,
        ):
            environment = module.PrebuiltE2BEnvironment()
            with self.assertRaisesRegex(RuntimeError, "force_build"):
                asyncio.run(environment.start(True))

        self.assertEqual(FakeAsyncSandbox.calls, [])

    def test_template_must_come_from_host_environment(self) -> None:
        module = load_module()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TB_E2B_PREBUILT_TEMPLATE"):
                module.PrebuiltE2BEnvironment()


if __name__ == "__main__":
    unittest.main()
