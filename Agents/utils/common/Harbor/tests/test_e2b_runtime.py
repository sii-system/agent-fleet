"""Tests for the explicit E2B sandbox timeout compatibility boundary."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "e2b_runtime.py"
SPEC = importlib.util.spec_from_file_location("fleet_e2b_runtime", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BuildException(Exception):
    pass


class SandboxException(Exception):
    pass


class FakeLogger:
    def debug(self, *args, **kwargs) -> None:
        pass


def fake_runtime_modules(environment_cls, template_cls, sandbox_cls=None):
    e2b_module = types.ModuleType("e2b")
    e2b_module.AsyncTemplate = template_cls
    e2b_module.AsyncSandbox = sandbox_cls or type("FakeAsyncSandbox", (), {})

    harbor_module = types.ModuleType("harbor")
    environments_module = types.ModuleType("harbor.environments")
    harbor_e2b_module = types.ModuleType("harbor.environments.e2b")
    harbor_e2b_module.E2BEnvironment = environment_cls
    harbor_module.environments = environments_module
    environments_module.e2b = harbor_e2b_module

    return {
        "e2b": e2b_module,
        "harbor": harbor_module,
        "harbor.environments": environments_module,
        "harbor.environments.e2b": harbor_e2b_module,
    }


def base_template_class():
    class FakeAsyncTemplate:
        aliases: ClassVar[set[str]] = set()
        tags: ClassVar[dict[str, list[str]]] = {}

        @classmethod
        async def alias_exists(cls, alias: str) -> bool:
            return alias in cls.aliases

        @classmethod
        async def get_tags(cls, alias: str):
            if alias not in cls.aliases:
                raise RuntimeError("404: template not found")
            return [SimpleNamespace(tag=tag) for tag in cls.tags.get(alias, [])]

    return FakeAsyncTemplate


def base_environment_class(template_cls):
    class FakeE2BEnvironment:
        build_calls = 0
        sandbox_calls = 0

        def __init__(self, alias: str = "task__hash") -> None:
            self._template_name = alias
            self.logger = FakeLogger()

        async def _does_template_exist(self) -> bool:
            return await template_cls.alias_exists(self._template_name)

        async def _create_template(self):
            type(self).build_calls += 1
            template_cls.aliases.add(self._template_name)
            await asyncio.sleep(0.03)
            template_cls.tags[self._template_name] = ["default"]
            return "build"

        async def _create_sandbox(self):
            type(self).sandbox_calls += 1
            return "sandbox"

        async def start(self, force_build: bool):
            if force_build or not await self._does_template_exist():
                await self._create_template()
            return await self._create_sandbox()

    return FakeE2BEnvironment


class E2BRuntimeTest(unittest.TestCase):
    def test_caps_harbor_requested_timeout(self) -> None:
        calls: list[int | None] = []

        class FakeAsyncSandbox:
            @classmethod
            async def create(cls, *args, **kwargs):
                calls.append(kwargs.get("timeout"))
                return "sandbox"

        fake_e2b = types.SimpleNamespace(AsyncSandbox=FakeAsyncSandbox)
        with patch.dict(sys.modules, {"e2b": fake_e2b}), patch.dict(
            os.environ,
            {"HARBOR_ENVIRONMENT_TYPE": "qz", "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "3600"},
        ):
            # The cap is E2B-specific; a qz worker inheriting the variable on
            # a mixed host must keep its own timeout handling.
            self.assertFalse(MODULE.patch_e2b_sandbox_timeout_from_env())
        with patch.dict(sys.modules, {"e2b": fake_e2b}), patch.dict(
            os.environ,
            {"HARBOR_ENVIRONMENT_TYPE": "e2b", "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "3600"},
        ):
            self.assertTrue(MODULE.patch_e2b_sandbox_timeout_from_env())
            result = asyncio.run(FakeAsyncSandbox.create(timeout=86_400))

        self.assertEqual(result, "sandbox")
        self.assertEqual(calls, [3600])

    def test_does_not_patch_sandbox_timeout_for_non_e2b_backend(self) -> None:
        calls: list[int | None] = []

        class FakeAsyncSandbox:
            @classmethod
            async def create(cls, *args, **kwargs):
                calls.append(kwargs.get("timeout"))
                return "sandbox"

        fake_e2b = types.SimpleNamespace(AsyncSandbox=FakeAsyncSandbox)
        with patch.dict(sys.modules, {"e2b": fake_e2b}), patch.dict(
            os.environ,
            {
                "HARBOR_ENVIRONMENT_TYPE": "opensandbox",
                "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "3600",
            },
        ):
            self.assertFalse(MODULE.patch_e2b_sandbox_timeout_from_env())
            result = asyncio.run(FakeAsyncSandbox.create(timeout=86_400))

        self.assertEqual(result, "sandbox")
        self.assertEqual(calls, [86_400])

    def test_cold_alias_has_one_builder_and_four_sandboxes(self) -> None:
        template_cls = base_template_class()
        environment_cls = base_environment_class(template_cls)
        modules = fake_runtime_modules(environment_cls, template_cls)

        async def run_trials() -> None:
            environments = [environment_cls() for _ in range(4)]
            await asyncio.gather(*(environment.start(False) for environment in environments))

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, modules
        ), patch.dict(
            os.environ,
            {
                "HARBOR_ENVIRONMENT_TYPE": "e2b",
                "HARBOR_E2B_TEMPLATE_LOCK_DIR": temp_dir,
                "HARBOR_E2B_TEMPLATE_READY_TIMEOUT_SEC": "1",
                "HARBOR_E2B_TEMPLATE_POLL_INTERVAL_SEC": "0.01",
            },
        ):
            self.assertTrue(MODULE.patch_e2b_template_coordination_from_env())
            asyncio.run(run_trials())

        self.assertEqual(environment_cls.build_calls, 1)
        self.assertEqual(environment_cls.sandbox_calls, 4)

    def test_build_race_waits_for_external_winner(self) -> None:
        template_cls = base_template_class()
        environment_cls = base_environment_class(template_cls)

        async def losing_build(self):
            type(self).build_calls += 1
            raise BuildException("400: build is not in waiting state")

        environment_cls._create_template = losing_build
        modules = fake_runtime_modules(environment_cls, template_cls)

        async def run_trial() -> None:
            async def publish_winning_build() -> None:
                await asyncio.sleep(0.03)
                template_cls.aliases.add("task__hash")
                template_cls.tags["task__hash"] = ["default"]

            publisher = asyncio.create_task(publish_winning_build())
            await environment_cls().start(False)
            await publisher

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, modules
        ), patch.dict(
            os.environ,
            {
                "HARBOR_ENVIRONMENT_TYPE": "e2b",
                "HARBOR_E2B_TEMPLATE_LOCK_DIR": temp_dir,
                "HARBOR_E2B_TEMPLATE_READY_TIMEOUT_SEC": "1",
                "HARBOR_E2B_TEMPLATE_POLL_INTERVAL_SEC": "0.01",
            },
        ):
            MODULE.patch_e2b_template_coordination_from_env()
            asyncio.run(run_trial())

        self.assertEqual(environment_cls.build_calls, 1)
        self.assertEqual(environment_cls.sandbox_calls, 1)

    def test_missing_default_tag_retries_sandbox_create(self) -> None:
        template_cls = base_template_class()
        template_cls.aliases.add("task__hash")
        template_cls.tags["task__hash"] = ["default"]
        environment_cls = base_environment_class(template_cls)

        async def eventually_visible(self):
            type(self).sandbox_calls += 1
            if type(self).sandbox_calls == 1:
                raise SandboxException(
                    "404: tag 'default' does not exist for template 'task__hash'"
                )
            return "sandbox"

        environment_cls._create_sandbox = eventually_visible
        modules = fake_runtime_modules(environment_cls, template_cls)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, modules
        ), patch.dict(
            os.environ,
            {
                "HARBOR_ENVIRONMENT_TYPE": "e2b",
                "HARBOR_E2B_TEMPLATE_LOCK_DIR": temp_dir,
                "HARBOR_E2B_TEMPLATE_READY_TIMEOUT_SEC": "1",
                "HARBOR_E2B_TEMPLATE_POLL_INTERVAL_SEC": "0.01",
            },
        ):
            MODULE.patch_e2b_template_coordination_from_env()
            asyncio.run(environment_cls().start(False))

        self.assertEqual(environment_cls.build_calls, 0)
        self.assertEqual(environment_cls.sandbox_calls, 2)

    def test_real_build_error_is_not_hidden(self) -> None:
        template_cls = base_template_class()
        environment_cls = base_environment_class(template_cls)
        expected = BuildException("Dockerfile instruction failed")

        async def broken_build(self):
            raise expected

        environment_cls._create_template = broken_build
        modules = fake_runtime_modules(environment_cls, template_cls)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, modules
        ), patch.dict(
            os.environ,
            {
                "HARBOR_ENVIRONMENT_TYPE": "e2b",
                "HARBOR_E2B_TEMPLATE_LOCK_DIR": temp_dir,
                "HARBOR_E2B_TEMPLATE_READY_TIMEOUT_SEC": "1",
                "HARBOR_E2B_TEMPLATE_POLL_INTERVAL_SEC": "0.01",
            },
        ):
            MODULE.patch_e2b_template_coordination_from_env()
            with self.assertRaises(BuildException) as raised:
                asyncio.run(environment_cls().start(False))

        self.assertIs(raised.exception, expected)

    def test_uploads_offline_verifier_tools_after_sandbox_start(self) -> None:
        events: list[object] = []
        template_cls = base_template_class()

        class FakeE2BEnvironment:
            async def start(self, force_build: bool):
                events.append(("start", force_build))
                return "started"

            async def exec(self, command: str, user: str):
                events.append(("exec", command, user))
                return SimpleNamespace(return_code=0, stdout="", stderr="")

            async def upload_dir(self, source: Path, target: str):
                events.append(("upload", source, target))

        modules = fake_runtime_modules(FakeE2BEnvironment, template_cls)
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            for name in ("uv", "uvx", "curl"):
                (source / name).write_text(name)

            with patch.dict(sys.modules, modules), patch.dict(
                os.environ,
                {
                    "HARBOR_ENVIRONMENT_TYPE": "e2b",
                    "HARBOR_E2B_VERIFIER_UV_SOURCE": str(source),
                    "HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH": "/opt/test-uv/bin",
                },
            ):
                self.assertTrue(MODULE.patch_e2b_verifier_tools_from_env())
                result = asyncio.run(FakeE2BEnvironment().start(False))

        self.assertEqual(result, "started")
        self.assertEqual(events[0], ("start", False))
        self.assertEqual(events[1], ("exec", "mkdir -p -- /opt/test-uv/bin", "root"))
        self.assertEqual(events[2], ("upload", source, "/opt/test-uv/bin"))
        self.assertEqual(
            events[3],
            (
                "exec",
                "chmod 0755 -- /opt/test-uv/bin/uv /opt/test-uv/bin/uvx /opt/test-uv/bin/curl",
                "root",
            ),
        )

    def test_verifier_tools_patch_arms_for_qz_but_not_docker(self) -> None:
        class FakeE2BEnvironment:
            async def start(self, force_build: bool):
                return "started"

            async def exec(self, command, user=None):
                return types.SimpleNamespace(return_code=0, stdout="", stderr="")

            async def upload_dir(self, source: Path, target: str):
                return None

        modules = fake_runtime_modules(FakeE2BEnvironment, type("FakeTemplate", (), {}))
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            for name in ("uv", "uvx", "curl"):
                (source / name).write_text(name)

            common_env = {
                "HARBOR_E2B_VERIFIER_UV_SOURCE": str(source),
                "HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH": "/opt/test-uv/bin",
            }
            with patch.dict(sys.modules, modules), patch.dict(
                os.environ, {**common_env, "HARBOR_ENVIRONMENT_TYPE": "qz"}
            ):
                self.assertTrue(MODULE.patch_e2b_verifier_tools_from_env())
            with patch.dict(
                os.environ, {**common_env, "HARBOR_ENVIRONMENT_TYPE": "docker"}
            ):
                self.assertFalse(MODULE.patch_e2b_verifier_tools_from_env())

if __name__ == "__main__":
    unittest.main()
