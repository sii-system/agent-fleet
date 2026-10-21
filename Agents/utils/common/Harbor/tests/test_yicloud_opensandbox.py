import asyncio
import base64
import contextvars
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = HARBOR_DIR / "yicloud_opensandbox.py"
REGISTRY_HOST = "harbor.example.internal"
sys.path.insert(0, str(HARBOR_DIR))


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    base = types.ModuleType("harbor.environments.base")
    capabilities = types.ModuleType("harbor.environments.capabilities")

    class BaseEnvironment:
        default_user = None

        def _resolve_user(self, user):
            return user if user is not None else self.default_user

    class ExecResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Capability:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    base.BaseEnvironment = BaseEnvironment
    base.ExecResult = ExecResult
    capabilities.EnvironmentCapabilities = Capability
    capabilities.EnvironmentResourceCapabilities = Capability
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": environments,
            "harbor.environments.base": base,
            "harbor.environments.capabilities": capabilities,
        }
    )


install_harbor_stubs()
spec = importlib.util.spec_from_file_location("yicloud_opensandbox", MODULE_PATH)
assert spec is not None and spec.loader is not None
yicloud_opensandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yicloud_opensandbox)

from harbor_environment_adapters.providers.opensandbox_yicloud import (
    adapter as yicloud_adapter,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    client as yicloud_client,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    config as yicloud_config,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    execution as yicloud_execution,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    lifecycle as yicloud_lifecycle,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    services as yicloud_services,
)
from harbor_environment_adapters.providers.opensandbox_yicloud import (
    transfer as yicloud_transfer,
)


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class SandboxListingTest(unittest.IsolatedAsyncioTestCase):
    def instance(self, sandbox, project="project", environment="environment"):
        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        instance._sandbox_service = sandbox
        instance._project_name = project
        instance._environment_id = environment
        instance._cleanup_wait_sec = 1
        return instance

    def sandbox(self, list_page):
        return SimpleNamespace(
            models=SimpleNamespace(ListSandboxesReq=Request),
            list_sandboxes=Mock(side_effect=list_page),
        )

    async def test_concurrent_confirmations_share_a_paginated_environment_scan(self):
        items = [SimpleNamespace(Id=f"sbx-{index}") for index in range(10000)]
        sandbox = self.sandbox(lambda _, req: SimpleNamespace(
            Items=items[req.Offset:req.Offset + req.Limit],
        ))
        instances = [self.instance(sandbox) for _ in range(100)]
        results = await asyncio.gather(*(
            instance._listed_sandbox_ids({f"sbx-{index}", "deleted"})
            for index, instance in enumerate(instances)
        ))
        self.assertEqual(results, [{f"sbx-{index}"} for index in range(100)])
        self.assertEqual(sandbox.list_sandboxes.call_count, 101)

    async def test_caller_arriving_after_first_page_requires_a_fresh_scan(self):
        page_read, release = threading.Event(), threading.Event()
        created = False

        def list_page(_, req):
            if req.Offset == 100:
                page_read.set()
                if not release.wait(2):
                    raise TimeoutError("test page was not released")
                return SimpleNamespace(Items=[])
            if created:
                return SimpleNamespace(Items=[SimpleNamespace(Id="new-sandbox")])
            return SimpleNamespace(Items=[SimpleNamespace(Id=f"old-{i}") for i in range(100)])

        sandbox = self.sandbox(list_page)
        first = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"deleted"}))
        try:
            self.assertTrue(await asyncio.to_thread(page_read.wait, 1))
            created = True
            late = asyncio.create_task(
                self.instance(sandbox)._listed_sandbox_ids({"new-sandbox"}),
            )
            await asyncio.sleep(0)
        finally:
            release.set()
        self.assertEqual(await first, set())
        self.assertEqual(await late, {"new-sandbox"})

    async def test_listing_scopes_do_not_mix_services_projects_or_environments(self):
        def list_page(_, req):
            return SimpleNamespace(Items=[SimpleNamespace(Id=f"{req.ProjectName}/{req.EnvironmentId}")])

        sandbox = self.sandbox(list_page)
        other = self.sandbox(list_page)
        instances = [
            self.instance(sandbox), self.instance(sandbox),
            self.instance(sandbox, project="other"),
            self.instance(sandbox, environment="other"), self.instance(other),
        ]
        results = await asyncio.gather(*(
            instance._listed_sandbox_ids({"project/environment", "other/environment", "project/other"})
            for instance in instances
        ))
        self.assertEqual(results, [
            {"project/environment"}, {"project/environment"},
            {"other/environment"}, {"project/other"}, {"project/environment"},
        ])
        self.assertEqual(sandbox.list_sandboxes.call_count, 3)
        self.assertEqual(other.list_sandboxes.call_count, 1)

    async def test_followup_confirmation_does_not_reuse_a_completed_scan(self):
        items = []
        sandbox = self.sandbox(lambda *_: SimpleNamespace(Items=list(items)))
        instance = self.instance(sandbox)
        self.assertEqual(await instance._listed_sandbox_ids({"new-sandbox"}), set())
        items.append(SimpleNamespace(Id="new-sandbox"))
        self.assertEqual(await instance._listed_sandbox_ids({"new-sandbox"}), {"new-sandbox"})

    async def test_concurrent_confirmations_share_an_empty_environment_scan(self):
        sandbox = self.sandbox(lambda *_: SimpleNamespace(Items=[]))
        results = await asyncio.gather(*(
            self.instance(sandbox)._listed_sandbox_ids({f"deleted-{index}"})
            for index in range(100)
        ))
        self.assertEqual(results, [set() for _ in range(100)])
        self.assertEqual(sandbox.list_sandboxes.call_count, 1)

    async def test_failed_page_never_confirms_absence_and_next_call_can_retry(self):
        sandbox = self.sandbox(lambda *_: None)
        sandbox.list_sandboxes.side_effect = [
            SimpleNamespace(Items=[SimpleNamespace(Id=f"sbx-{i}") for i in range(100)]),
            ConnectionError("listing disconnected"), SimpleNamespace(Items=[]),
        ]
        instance = self.instance(sandbox)
        with self.assertRaisesRegex(ConnectionError, "listing disconnected"):
            await instance._listed_sandbox_ids({"deleted"})
        self.assertEqual(await instance._listed_sandbox_ids({"deleted"}), set())

    async def test_cancelled_waiter_does_not_cancel_another_confirmation(self):
        started, release = threading.Event(), threading.Event()

        def list_page(*_):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test page was not released")
            return SimpleNamespace(Items=[SimpleNamespace(Id="still-present")])

        sandbox = self.sandbox(list_page)
        first = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"still-present"}))
        survivor = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"still-present"}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        finally:
            release.set()
        self.assertEqual(await survivor, {"still-present"})

    async def test_cancelled_queued_waiter_leaves_the_scan_available(self):
        started, release = threading.Event(), threading.Event()

        def list_page(*_):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test page was not released")
            return SimpleNamespace(Items=[])

        sandbox = self.sandbox(list_page)
        owner = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"deleted"}))
        queued = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"deleted"}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
        finally:
            release.set()
        self.assertEqual(await owner, set())
        self.assertEqual(sandbox.list_sandboxes.call_count, 1)

    async def test_cleanup_budget_bounds_a_slow_scan_without_cancelling_peers(self):
        started, release = threading.Event(), threading.Event()

        def list_page(*_):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test page was not released")
            return SimpleNamespace(Items=[])

        sandbox = self.sandbox(list_page)
        instance = self.instance(sandbox)
        instance._cleanup_wait_sec = 0.05
        first = asyncio.create_task(instance._wait_for_sandbox_ids_absent({"deleted"}))
        survivor = asyncio.create_task(self.instance(sandbox)._listed_sandbox_ids({"deleted"}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.assertFalse(await asyncio.wait_for(first, 0.5))
        finally:
            release.set()
        self.assertEqual(await survivor, set())

    async def test_cleanup_does_not_swallow_an_sdk_timeout(self):
        sandbox = self.sandbox(lambda *_: None)
        sandbox.list_sandboxes.side_effect = TimeoutError("SDK request timed out")
        with self.assertRaisesRegex(TimeoutError, "SDK request timed out"):
            await self.instance(sandbox)._wait_for_sandbox_ids_absent({"deleted"})


class DirectoryPackingDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_native_async_offloads_http_packing(self):
        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        instance._upload_file_http = AsyncMock()
        instance.exec = AsyncMock(return_value=SimpleNamespace(return_code=0))
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / "data").write_text("payload")
            for native, mode in (("1", "sync"), ("0", "async"), ("1", "async")):
                with self.subTest(native=native, mode=mode), patch.dict(os.environ, {
                    "HARBOR_NATIVE_CONCURRENCY": native,
                    "HARBOR_OPENSANDBOX_COMMAND_MODE": mode,
                }), patch.object(yicloud_transfer, "pack_directory_async",
                    new=AsyncMock(side_effect=lambda src, dst: dst.write_bytes(b"archive"))) as pack:
                    await instance._upload_dir_http(source, "/opt/deps")
                    self.assertEqual(pack.await_count, int(native == "1" and mode == "async"))


class NativeCommandPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_configures_shared_auxiliary_threads_before_sdk_calls(self):
        loop = asyncio.get_running_loop()
        admitted, release = asyncio.Event(), threading.Event()
        lock = threading.Lock()
        started = 0

        def work():
            nonlocal started
            with lock:
                started += 1
                if started == 3:
                    loop.call_soon_threadsafe(admitted.set)
            if not release.wait(5):
                raise TimeoutError("auxiliary worker was not released")

        async def control_call(*_):
            tasks = [asyncio.create_task(asyncio.to_thread(work)) for _ in range(6)]
            try:
                await asyncio.wait_for(admitted.wait(), 1)
                # A sibling trial must keep using this event loop's existing pool.
                yicloud_opensandbox._configure_auxiliary_pool()
                tasks.append(asyncio.create_task(asyncio.to_thread(work)))
                await asyncio.sleep(.02)
                self.assertEqual(started, 3)
            finally:
                release.set()
                await asyncio.gather(*tasks)
            raise RuntimeError("control-plane probe complete")

        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        instance._validate_definition = instance._initialize_service = Mock()
        instance._sandbox_service = object()
        instance._retry_control_plane_auth = control_call
        with patch.dict(os.environ, HARBOR_OPENSANDBOX_AUX_THREADS="3"), \
                self.assertRaisesRegex(RuntimeError, "control-plane probe complete"):
            await instance.start(False)

    async def test_invalid_auxiliary_threads_fail_before_startup(self):
        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, HARBOR_OPENSANDBOX_AUX_THREADS=value), \
                    self.assertRaisesRegex(ValueError, "HARBOR_OPENSANDBOX_AUX_THREADS"):
                await instance.start(False)

    async def test_native_async_128_commands_bypass_command_pool_and_preserve_context(self):
        context = contextvars.ContextVar("native-task")
        release = asyncio.Event()
        admitted = asyncio.Event()
        started = []
        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        instance.task_env_config = SimpleNamespace(workdir="/app")
        instance._merge_env = lambda env: env or {}
        instance._output_callback = lambda: None

        async def command(*args):
            started.append(context.get())
            if len(started) == 128:
                admitted.set()
            await release.wait()
            return SimpleNamespace(stdout=context.get(), stderr="", return_code=0)

        instance._run_command_async = command
        instance._run_command_sync = Mock(side_effect=AssertionError("sync replay"))
        with patch.dict(os.environ, HARBOR_NATIVE_CONCURRENCY="1", HARBOR_N_CONCURRENT="128",
                        HARBOR_OPENSANDBOX_COMMAND_MODE="async"):
            tasks = []
            try:
                for value in range(128):
                    context.set(str(value))
                    tasks.append(asyncio.create_task(instance.exec("test")))
                await asyncio.wait_for(admitted.wait(), 5)
                self.assertEqual(started, [str(i) for i in range(128)])
                release.set()
                results = await asyncio.gather(*tasks)
                self.assertEqual([r.stdout for r in results], [str(i) for i in range(128)])
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_commands_do_not_starve_control_plane_and_preserve_context(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        context = contextvars.ContextVar("task")
        started = threading.Barrier(5)
        release = threading.Event()
        instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        instance.task_env_config = SimpleNamespace(workdir="/app")
        instance._merge_env = lambda env: env or {}
        instance._output_callback = lambda: None

        def command(*args):
            started.wait(timeout=10)
            release.wait(timeout=10)
            return SimpleNamespace(stdout=context.get(), stderr="", exit_code=0)

        instance._run_command_sync = command
        with patch.dict(os.environ, HARBOR_NATIVE_CONCURRENCY="1", HARBOR_N_CONCURRENT="4"):
            try:
                tasks = []
                for value in range(4):
                    context.set(str(value))
                    tasks.append(asyncio.create_task(instance.exec("test")))
                await asyncio.wait_for(asyncio.to_thread(started.wait, 10), 12)
                self.assertEqual(await asyncio.wait_for(asyncio.to_thread(lambda: "control"), 1), "control")
                release.set()
                results = await asyncio.gather(*tasks)
                self.assertEqual([result.stdout for result in results], ["0", "1", "2", "3"])
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                yicloud_execution._native_command_pool().shutdown(wait=True)
                yicloud_execution._native_command_pool.cache_clear()


def make_healthcheck_environment(ready_timeout_sec: int):
    instance = object.__new__(
        yicloud_opensandbox.YiCloudOpenSandboxEnvironment
    )
    runtime = yicloud_opensandbox.ServiceRuntime(
        "worker",
        {
            "runtime": {
                "readiness": {
                    "type": "healthcheck",
                    "healthcheck": {"test": "check-ready"},
                }
            }
        },
    )
    instance._services = {runtime.name: runtime}
    instance._main_service = runtime.name
    instance._ready_timeout_sec = ready_timeout_sec
    return instance


class FakeSandbox:
    def __init__(self, environments):
        self.environments = environments
        self.models = SimpleNamespace(
            ListSandboxEnvironmentsReq=Request,
            GetSandboxEnvironmentReq=Request,
        )

    def list_sandbox_environments(self, _context, _request):
        return SimpleNamespace(Items=self.environments)

    def get_sandbox_environment(self, _context, request):
        return next(
            (
                item
                for item in self.environments
                if item.Id == request.EnvironmentId
            ),
            SimpleNamespace(Id="", Name=""),
        )


class YiCloudOpenSandboxTest(unittest.TestCase):
    def test_single_start_logs_ready_latency_from_create_to_execd(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.logger = Mock()
        instance._bundle = None
        instance._sandbox_name = ""
        instance.session_id = "test-session"
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance._environment_name = "test-environment"
        instance._image_ref_override = "repo/image@sha256:" + "a" * 64
        instance.task_env_config = SimpleNamespace(docker_image="")
        instance._persistent_env = {}
        instance._request_cpu = "1"
        instance._request_memory = "2Gi"
        instance._lifecycle_minutes = 120
        instance._request_timeout_sec = 180
        instance._ready_timeout_sec = 300
        instance._retain_on_start_failure = False
        instance._validate_definition = Mock()
        instance._initialize_service = Mock()
        instance._resolve_environment_id = Mock()
        instance._create_image_input = Mock(return_value=Request())
        created = SimpleNamespace(Id="sbx-test", AccessToken="token")
        current = SimpleNamespace(
            EnvironmentId="env-test",
            Image=SimpleNamespace(Ref=instance._image_ref),
            Endpoints=SimpleNamespace(
                Endpoints={
                    "exec": SimpleNamespace(
                        ProxyUrl="https://sandbox.example/command"
                    )
                }
            ),
        )
        instance._sandbox_service = SimpleNamespace(
            create_sandbox=Mock(),
            models=SimpleNamespace(
                CreateSandboxReq=Request,
                CreateSandboxReqResources=Request,
            ),
        )
        instance._retry_control_plane_auth = AsyncMock(
            side_effect=[None, created]
        )
        instance._wait_until_running = AsyncMock(return_value=current)
        instance._wait_until_execd_ready = AsyncMock()
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )
        instance._upload_environment_dir_after_start = AsyncMock()
        instance._materialize_read_only_mounts = AsyncMock()
        instance._handle_start_failure = AsyncMock()

        with patch.object(
            yicloud_lifecycle,
            "_monotonic",
            side_effect=[10.0, 10.125, 10.375, 10.5],
        ):
            asyncio.run(instance.start(force_build=False))

        ready_logs = [
            log for log in instance.logger.info.call_args_list
            if log.args and str(log.args[0]).startswith("YiCloud Sandbox ready")
        ]
        self.assertEqual(len(ready_logs), 1)
        self.assertEqual(ready_logs[0].args[-2:], (125.0, 375.0))
        self.assertEqual(instance._create_request_started_at, 10.0)

    def test_sdk_version_requires_region_aware_client(self):
        with patch.object(yicloud_client, "version", return_value="0.4.1"):
            yicloud_client._require_yicloud_sdk()
        with (
            patch.object(yicloud_client, "version", return_value="0.3.1"),
            self.assertRaisesRegex(RuntimeError, "expected yicloud-sdk-python==0.4.1"),
        ):
            yicloud_client._require_yicloud_sdk()

    def test_exec_runtime_keeps_missing_environment_as_none(self) -> None:
        command, env = yicloud_execution._prepare_exec_runtime("true", None)

        self.assertEqual(command, "true")
        self.assertIsNone(env)

    def test_verifier_runtime_prepends_without_replacing_image_path(self) -> None:
        wrapped, env = yicloud_execution._prepare_exec_runtime(
            'printf "%s\\n" "$PATH"',
            {
                yicloud_execution.VERIFIER_PATH_PREPEND_ENV: "/verifier/bin",
                "KEEP": "value",
            },
        )

        completed = subprocess.run(
            ["/bin/sh", "-c", wrapped],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/image/runtime/bin:/usr/bin:/bin", **env},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "/verifier/bin:/image/runtime/bin:/usr/bin:/bin",
        )
        self.assertEqual(env, {"KEEP": "value"})

    def test_verifier_runtime_materializes_portable_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source" / "python3.12-runtime"
            source_bin = source_root / "bin"
            source_bin.mkdir(parents=True)
            python = source_bin / "python3.12"
            python.write_text("#!/bin/sh\nprintf 'portable-python\\n'\n")
            python.chmod(0o755)
            (source_bin / "python3").symlink_to("python3.12")
            (source_bin / "python").symlink_to("python3.12")
            check = source_bin / "harbor-verifier-bundle-check"
            check.write_text('#!/bin/sh\nexec "${0%/*}/python3" >/dev/null\n')
            check.chmod(0o755)
            archive = root / "python3.12-runtime.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(source_root, arcname=source_root.name)

            runtime_root = root / "runtime" / source_root.name
            tools = root / "tools"
            tools.mkdir()
            for name in ("gzip", "mkdir", "rm", "tar"):
                target = shutil.which(name)
                self.assertIsNotNone(target)
                os.symlink(target, tools / name)
            image_python = tools / "python3"
            image_python.write_text("#!/bin/sh\nprintf 'image-python\\n'\n")
            image_python.chmod(0o755)

            wrapped, env = yicloud_execution._prepare_exec_runtime(
                "command -v python3 && python3",
                {
                    yicloud_execution.VERIFIER_PATH_PREPEND_ENV: str(
                        runtime_root / "bin"
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ID_ENV: (
                        "test-python-verifier-bundle"
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV: str(
                        archive
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ROOT_ENV: str(
                        runtime_root
                    ),
                },
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", wrapped],
                capture_output=True,
                text=True,
                check=False,
                env={"PATH": str(tools), **env},
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [str(runtime_root / "bin" / "python3"), "portable-python"],
        )

    def test_verifier_runtime_replaces_agent_writable_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source" / "python3.12-runtime"
            source_bin = source_root / "bin"
            source_bin.mkdir(parents=True)
            python = source_bin / "python3.12"
            python.write_text("#!/bin/sh\nprintf 'portable-python\\n'\n")
            python.chmod(0o755)
            (source_bin / "python3").symlink_to("python3.12")
            (source_bin / "python").symlink_to("python3.12")
            check = source_bin / "harbor-verifier-bundle-check"
            check.write_text('#!/bin/sh\nexec "${0%/*}/python3" >/dev/null\n')
            check.chmod(0o755)
            archive = root / "python3.12-runtime.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(source_root, arcname=source_root.name)

            runtime_root = root / "runtime" / "python3.12-runtime"
            runtime_bin = runtime_root / "bin"
            runtime_bin.mkdir(parents=True)
            python = runtime_bin / "python3.12"
            python.write_text("#!/bin/sh\nprintf 'agent-python\\n'\n")
            python.chmod(0o755)
            (runtime_bin / "python3").symlink_to("python3.12")
            (runtime_bin / "python").symlink_to("python3.12")
            check = runtime_bin / "harbor-verifier-bundle-check"
            check.write_text("#!/bin/sh\nexit 0\n")
            check.chmod(0o755)
            (runtime_root / ".harbor-verifier-bundle-materialized-v1").touch()

            tools = root / "tools"
            tools.mkdir()
            for name in ("gzip", "mkdir", "rm", "tar"):
                target = shutil.which(name)
                self.assertIsNotNone(target)
                os.symlink(target, tools / name)

            wrapped, env = yicloud_execution._prepare_exec_runtime(
                "python3",
                {
                    yicloud_execution.VERIFIER_PATH_PREPEND_ENV: str(
                        runtime_bin
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ID_ENV: (
                        "test-python-verifier-bundle"
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV: str(
                        archive
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ROOT_ENV: str(
                        runtime_root
                    ),
                },
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", wrapped],
                capture_output=True,
                text=True,
                check=False,
                env={"PATH": str(tools), **(env or {})},
            )
            legacy_marker_exists = (
                runtime_root / ".harbor-verifier-bundle-materialized-v1"
            ).exists()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "portable-python")
        self.assertFalse(legacy_marker_exists)

    def test_verifier_runtime_bundle_settings_must_be_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be configured together"):
            yicloud_execution._prepare_exec_runtime(
                "true",
                {
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV: (
                        "/runtime.tar.gz"
                    )
                },
            )

    def test_missing_verifier_runtime_bundle_stops_before_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "tools"
            tools.mkdir()
            wrapped, env = yicloud_execution._prepare_exec_runtime(
                "printf 'command-ran\\n'",
                {
                    yicloud_execution.VERIFIER_PATH_PREPEND_ENV: str(tools),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ID_ENV: (
                        "test-python-verifier-bundle"
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV: str(
                        root / "missing.tar.gz"
                    ),
                    yicloud_execution.VERIFIER_RUNTIME_BUNDLE_ROOT_ENV: str(
                        root / "runtime" / "python3.12-runtime"
                    ),
                },
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", wrapped],
                capture_output=True,
                text=True,
                check=False,
                env={"PATH": str(tools), **(env or {})},
            )

        self.assertEqual(completed.returncode, 127)
        self.assertNotIn("command-ran", completed.stdout)
        self.assertIn("runtime bundle archive is missing", completed.stderr)
        self.assertIn("/logs/verifier/runtime-bootstrap.log", wrapped)

    def test_v2_bundle_loads_digest_refs_and_rejects_unsupported_capabilities(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "bundle.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "main": "main",
                        "services": {
                            "main": {
                                "image": {
                                    "digest_ref": (
                                        f"{REGISTRY_HOST}/seta/0@sha256:"
                                        + "a" * 64
                                    )
                                }
                            },
                            "worker": {
                                "image": {
                                    "digest_ref": (
                                        f"{REGISTRY_HOST}/seta/0@sha256:"
                                        + "b" * 64
                                    )
                                }
                            },
                        },
                        "requirements": {"multi_service": True},
                    }
                ),
                encoding="utf-8",
            )
            instance._bundle_manifest_path = str(manifest)
            instance._bundle = None
            instance._services = {}
            instance._main_service = "main"
            with patch.dict(
                os.environ,
                {"YICLOUD_HARBOR_HOST": REGISTRY_HOST},
            ):
                instance._load_bundle()

        self.assertEqual(instance._main_service, "main")
        self.assertEqual(set(instance._services), {"main", "worker"})

        instance._bundle["requirements"] = {"fixed_ip": True}
        with self.assertRaisesRegex(RuntimeError, "capability gate rejected"):
            instance._capability_gate()

    def test_image_refs_must_use_configured_registry(self) -> None:
        digest = "sha256:" + "a" * 64
        with patch.dict(
            os.environ,
            {"YICLOUD_HARBOR_HOST": REGISTRY_HOST},
        ):
            yicloud_config._validate_managed_image_ref(
                f"{REGISTRY_HOST}/seta/0@{digest}",
                "test image",
            )
            for ref in (
                f"registry.gate.yicloud.com.cn/seta/0@{digest}",
                f"seta/0@{digest}",
            ):
                with self.subTest(ref=ref), self.assertRaisesRegex(
                    RuntimeError,
                    "must use the configured OpenSandbox registry",
                ):
                    yicloud_config._validate_managed_image_ref(
                        ref,
                        "test image",
                    )

    def test_legacy_image_override_rejects_other_registry(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._bundle_manifest_path = ""
        instance._bundle = None
        instance._services = {}
        instance._main_service = "main"
        instance._image_ref_override = (
            "registry.gate.yicloud.com.cn/seta/0@sha256:" + "a" * 64
        )
        instance.task_env_config = SimpleNamespace(docker_image=None)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {"YICLOUD_HARBOR_HOST": REGISTRY_HOST},
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "must use the configured OpenSandbox registry",
            ),
        ):
            instance.environment_dir = Path(tmp)
            instance._validate_definition()

    def test_service_port_parsing_uses_compose_container_ports(self) -> None:
        self.assertEqual(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_ports(
                {"ports": ["127.0.0.1:8080:80/tcp", "5432", {"target": 22}]}
            ),
            [80, 5432, 22],
        )

    def test_service_start_command_preserves_compose_argv(self) -> None:
        command = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_start_command
        self.assertEqual(command({"command": ["sh", "-c", "sleep infinity"]}), ["sh", "-c", "sleep infinity"])
        self.assertEqual(command({"entrypoint": ["/entry"], "command": ["--serve"]}), ["/entry", "--serve"])
        self.assertIsNone(command({}))

    def test_runtime_contract_is_preferred_over_legacy_compose_fields(self) -> None:
        start = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_start_command
        ports = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_ports
        spec = {
            "command": ["incorrect", "legacy"],
            "runtime": {
                "start_argv": ["/usr/sbin/sshd", "-D"],
                "internal_ports": [
                    {"port": 22, "protocol": "tcp", "source": "image-config.exposed-ports"}
                ],
            },
        }
        self.assertEqual(start(spec), ["/usr/sbin/sshd", "-D"])
        self.assertEqual(ports(spec), [22])
        with self.assertRaisesRegex(RuntimeError, "non-empty string list"):
            start({"runtime": {"start_argv": []}})

    def test_shell_only_image_cmd_is_kept_on_opensandbox_hold(self) -> None:
        start = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_start_command
        self.assertIsNone(
            start(
                {
                    "runtime": {
                        "start_argv": ["/bin/bash"],
                        "start_argv_source": "image-config.cmd",
                    }
                }
            )
        )
        self.assertEqual(
            start(
                {
                    "runtime": {
                        "start_argv": ["/bin/bash"],
                        "start_argv_source": "compose.command",
                    }
                }
            ),
            ["/bin/bash"],
        )

    def test_all_opensandbox_services_use_sleep_infinity_entrypoint(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "aliases": ["database"],
                "depends_on": {},
                "runtime": {"start_argv": ["worker"]},
            },
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main",
            {
                "depends_on": {},
                "runtime": {"start_argv": ["main"]},
            },
        )
        main.sandbox_name = "test-main"
        worker.sandbox_name = "test-worker"
        instance._services = {"main": main, "worker": worker}

        for runtime in (main, worker):
            self.assertEqual(
                instance._service_entrypoint(runtime),
                ["sleep", "infinity"],
            )

    def test_release_service_entrypoint_starts_real_command_via_execd(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "runtime": {
                    "start_argv": ["worker", "--serve"],
                    "workdir": "/srv/worker",
                }
            },
        )
        runtime.sandbox_name = "test-worker"
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        asyncio.run(instance._release_service_entrypoint(runtime))

        instance._run_service_command.assert_awaited_once_with(
            runtime,
            "nohup worker --serve >/tmp/harbor-service-worker.log 2>&1 "
            "</dev/null & pid=$!; echo $pid >/tmp/harbor-service-worker.pid; "
            "sleep 1; "
            "if ! kill -0 $pid 2>/dev/null; then "
            "tail -n 50 /tmp/harbor-service-worker.log >&2; exit 1; fi",
            cwd="/srv/worker",
            timeout_sec=60,
        )

    def test_release_service_entrypoint_raises_on_startup_crash(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {"runtime": {"start_argv": ["worker", "--serve"]}},
        )
        runtime.sandbox_name = "test-worker"
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(
                return_code=1, stdout="", stderr="exec: worker: not found"
            )
        )

        with self.assertRaisesRegex(RuntimeError, "worker: not found"):
            asyncio.run(instance._release_service_entrypoint(runtime))

    def test_release_service_entrypoint_leaves_commandless_image_running(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("main", {})
        instance._run_service_command = AsyncMock()

        asyncio.run(instance._release_service_entrypoint(runtime))

        instance._run_service_command.assert_not_awaited()

    def test_release_service_entrypoint_rejects_relative_workdir(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "runtime": {
                    "start_argv": ["worker", "--serve"],
                    "workdir": "relative/path",
                }
            },
        )
        instance._run_service_command = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "invalid runtime workdir"):
            asyncio.run(instance._release_service_entrypoint(runtime))

        instance._run_service_command.assert_not_awaited()

    def test_service_hosts_block_uses_real_newlines(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main", {"aliases": ["api", "main"]}
        )
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker", {"aliases": ["database"]}
        )
        main.internal_address = "10.0.0.1"
        worker.internal_address = "10.0.0.2"
        instance._services = {"main": main, "worker": worker}
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        asyncio.run(instance._wire_service_aliases())

        command = instance._run_service_command.await_args_list[0].args[1]
        encoded = command.split("printf %s ", 1)[1].split(" | base64", 1)[0]
        block = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(
            block,
            "10.0.0.1 main api\n10.0.0.2 worker database",
        )
        self.assertNotIn("\\n", block)
        self.assertIn(
            f"printf '\\n{yicloud_services.HOSTS_BLOCK_END}\\n'",
            command,
        )
        with tempfile.TemporaryDirectory() as tmp:
            hosts_path = Path(tmp) / "hosts"
            hosts_path.write_text("127.0.0.1 localhost\n", encoding="utf-8")
            test_command = command.replace(
                "/etc/hosts", shlex.quote(str(hosts_path))
            )
            rendered_hosts = []
            for _ in range(2):
                subprocess.run(
                    ["sh", "-c", test_command],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                rendered_hosts.append(hosts_path.read_text(encoding="utf-8"))
            self.assertEqual(rendered_hosts[0], rendered_hosts[1])
            self.assertEqual(
                rendered_hosts[-1],
                "127.0.0.1 localhost\n"
                f"{yicloud_services.HOSTS_BLOCK_BEGIN}\n"
                "10.0.0.1 main api\n"
                "10.0.0.2 worker database\n"
                f"{yicloud_services.HOSTS_BLOCK_END}\n",
            )

    def test_bundle_healthcheck_retries_until_success(self) -> None:
        instance = make_healthcheck_environment(300)
        instance._run_service_command = AsyncMock(
            side_effect=[
                SimpleNamespace(return_code=1, stdout="", stderr="starting"),
                SimpleNamespace(return_code=0, stdout="ready", stderr=""),
            ]
        )
        sleep = AsyncMock()

        with patch.object(yicloud_execution.asyncio, "sleep", sleep):
            asyncio.run(instance._wait_bundle_ready())

        self.assertEqual(instance._run_service_command.await_count, 2)
        sleep.assert_awaited_once_with(2)

    def test_bundle_healthcheck_respects_global_ready_deadline(self) -> None:
        instance = make_healthcheck_environment(3)
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=1, stdout="", stderr="")
        )
        clock = SimpleNamespace(value=0.0)

        async def advance(seconds: float) -> None:
            clock.value += seconds

        fake_time = SimpleNamespace(monotonic=lambda: clock.value)
        with (
            patch.object(yicloud_services, "time", fake_time),
            patch.object(
                yicloud_execution.asyncio,
                "sleep",
                side_effect=advance,
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "failed.*within 3s"),
        ):
            asyncio.run(instance._wait_bundle_ready())

        self.assertEqual(
            instance._run_service_command.await_count,
            2,
        )
        self.assertEqual(
            [item.args[0] for item in sleep.await_args_list],
            [2, 1.0],
        )

    def test_composite_starts_dependency_before_dependent_health_gate(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        events = []
        entrypoints = {}
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "image": {"digest_ref": "seta/973@sha256:" + "b" * 64},
                "depends_on": {},
                "runtime": {
                    "start_argv": ["worker"],
                    "internal_ports": [
                        {"port": 8080, "protocol": "tcp"}
                    ],
                    "readiness": {
                        "type": "healthcheck",
                        "healthcheck": {"test": "check-worker"},
                    },
                },
            },
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main",
            {
                "image": {"digest_ref": "seta/973@sha256:" + "a" * 64},
                "depends_on": {
                    "worker": {
                        "condition": "service_healthy",
                        "required": True,
                    }
                },
                "runtime": {
                    "start_argv": ["main"],
                    "internal_ports": [],
                    "readiness": None,
                },
            },
        )

        def create_sandbox(_context, request):
            service = request.Name.rsplit("-", 1)[-1]
            events.append(f"create:{service}")
            entrypoints[service] = request.Entrypoint
            return SimpleNamespace(
                Id=f"sbx-{service}", AccessToken=f"token-{service}"
            )

        async def wait_running(runtime, _created):
            events.append(f"running:{runtime.name}")
            port = 8080 if runtime.name == "worker" else 44772
            return SimpleNamespace(
                EnvironmentId="env-test",
                Image=SimpleNamespace(
                    Ref=instance._service_image_ref(runtime)
                ),
                Endpoints=SimpleNamespace(
                    Endpoints={
                        "exec": SimpleNamespace(
                            ProxyUrl=(
                                f"https://sandbox.example/{runtime.name}/ping"
                            ),
                            InternalUrl=f"tcp://10.0.0.2:{port}",
                        )
                    }
                ),
            )

        async def run_service_command(runtime, command, **_kwargs):
            events.append(f"health:{runtime.name}:{command}")
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        async def wait_execd(runtime):
            events.append(f"execd:{runtime.name}")

        instance._services = {"main": main, "worker": worker}
        instance._main_service = "main"
        instance._sandbox_service = SimpleNamespace(
            create_sandbox=create_sandbox,
            models=SimpleNamespace(
                CreateSandboxReq=Request,
                CreateSandboxReqImageInput=Request,
                CreateSandboxReqResources=Request,
                CreateSandboxReqPort=Request,
            ),
        )
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance._persistent_env = {}
        instance._request_cpu = "2"
        instance._request_memory = "8Gi"
        instance._lifecycle_minutes = 120
        instance._request_timeout_sec = 180
        instance._ready_timeout_sec = 300
        instance.session_id = "test-session"
        instance._wait_service_running = wait_running
        instance._wait_until_execd_ready = AsyncMock(side_effect=wait_execd)

        async def wire_aliases(*_args, **_kwargs):
            events.append("wire:all")

        async def release(runtime):
            events.append(f"release:{runtime.name}")

        instance._wire_service_aliases = AsyncMock(side_effect=wire_aliases)
        instance._release_service_entrypoint = AsyncMock(side_effect=release)
        instance._run_service_command = AsyncMock(
            side_effect=run_service_command
        )

        asyncio.run(instance._start_composite())

        self.assertEqual(
            events,
            [
                "create:worker",
                "running:worker",
                "execd:worker",
                "create:main",
                "running:main",
                "execd:main",
                "wire:all",
                "release:worker",
                "health:worker:check-worker",
                "release:main",
            ],
        )
        self.assertEqual(instance._sandbox_id, "sbx-main")
        self.assertEqual(main.state, "READY")
        self.assertEqual(worker.state, "READY")
        self.assertEqual(entrypoints["worker"], ["sleep", "infinity"])
        self.assertEqual(entrypoints["main"], ["sleep", "infinity"])
        self.assertEqual(worker.internal_address, "10.0.0.2")
        self.assertEqual(main.internal_address, "10.0.0.2")

    def test_composite_rejects_unsupported_dependency_before_create(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._services = {
            "main": yicloud_opensandbox.ServiceRuntime(
                "main",
                {
                    "depends_on": {
                        "worker": {
                            "condition": "service_completed_successfully",
                            "required": True,
                        }
                    }
                },
            ),
            "worker": yicloud_opensandbox.ServiceRuntime(
                "worker", {"depends_on": {}}
            ),
        }
        create_sandbox = Mock()
        instance._sandbox_service = SimpleNamespace(
            create_sandbox=create_sandbox
        )

        with self.assertRaisesRegex(
            RuntimeError, "dependency condition is unsupported"
        ):
            asyncio.run(instance._start_composite())

        create_sandbox.assert_not_called()

    def test_stop_service_deletes_sidecar_and_invalidates_connection(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.command_url = "https://sandbox.example/command"
        runtime.access_token = "sandbox-token"
        runtime.internal_address = "10.0.0.2"
        runtime.state = "READY"
        instance._services = {"worker": runtime}
        instance._main_service = "main"
        instance._cleanup_wait_sec = 30
        instance.logger = Mock()
        instance._delete_single_sandbox = AsyncMock(return_value=True)

        asyncio.run(instance.stop_service("worker"))

        instance._delete_single_sandbox.assert_awaited_once_with("sbx-worker")
        self.assertEqual(runtime.state, "DELETED")
        self.assertEqual(runtime.sandbox_id, "")
        self.assertEqual(runtime.command_url, "")
        self.assertEqual(runtime.access_token, "")
        self.assertEqual(runtime.internal_address, "")

    def test_stop_service_preserves_id_when_deletion_is_unconfirmed(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.command_url = "https://sandbox.example/command"
        runtime.access_token = "sandbox-token"
        runtime.internal_address = "10.0.0.2"
        runtime.state = "READY"
        instance._services = {"worker": runtime}
        instance._main_service = "main"
        instance._cleanup_wait_sec = 30
        instance.logger = Mock()
        instance._delete_single_sandbox = AsyncMock(return_value=False)

        asyncio.run(instance.stop_service("worker"))

        self.assertEqual(runtime.state, "DELETE_UNCONFIRMED")
        self.assertEqual(runtime.sandbox_id, "sbx-worker")
        self.assertEqual(runtime.command_url, "")
        self.assertEqual(runtime.access_token, "")
        self.assertEqual(runtime.internal_address, "")
        instance.logger.warning.assert_called_once()

    def test_single_sandbox_delete_uses_dedicated_provider_api(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        delete_sandbox = Mock()
        instance._sandbox_service = SimpleNamespace(
            delete_sandbox=delete_sandbox,
            models=SimpleNamespace(DeleteSandboxReq=Request),
        )
        instance._project_name = "test-project"
        instance._wait_for_sandbox_ids_absent = AsyncMock(return_value=True)

        deleted = asyncio.run(instance._delete_single_sandbox("sbx-worker"))

        self.assertTrue(deleted)
        delete_sandbox.assert_called_once()
        request = delete_sandbox.call_args.args[1]
        self.assertEqual(request.ProjectName, "test-project")
        self.assertEqual(request.SandboxId, "sbx-worker")
        instance._wait_for_sandbox_ids_absent.assert_awaited_once_with(
            {"sbx-worker"}
        )

    def test_service_group_uses_batch_only_for_multiple_ids(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        main = yicloud_opensandbox.ServiceRuntime("main", {})
        worker = yicloud_opensandbox.ServiceRuntime("worker", {})
        main.sandbox_id = "sbx-main"
        worker.sandbox_id = "sbx-worker"
        instance._services = {"main": main, "worker": worker}
        instance._delete_single_sandbox = AsyncMock(return_value=True)
        instance._batch_delete_sandboxes = AsyncMock(return_value=True)
        instance._detach_sandbox = Mock()

        asyncio.run(instance._delete_service_group())

        instance._batch_delete_sandboxes.assert_awaited_once_with(
            {"sbx-main", "sbx-worker"}
        )
        instance._delete_single_sandbox.assert_not_awaited()

        worker.sandbox_id = ""
        instance._batch_delete_sandboxes.reset_mock()
        asyncio.run(instance._delete_service_group())

        instance._delete_single_sandbox.assert_awaited_once_with("sbx-main")
        instance._batch_delete_sandboxes.assert_not_awaited()

    def test_yicloud_image_ref_strips_only_registry_host(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            yicloud_client._yicloud_image_ref(
                f"harbor.example/seta/973@{digest}"
            ),
            f"seta/973@{digest}",
        )
        self.assertEqual(
            yicloud_client._yicloud_image_ref(f"seta/973@{digest}"),
            f"seta/973@{digest}",
        )

    def test_full_oci_ref_is_sent_via_yicloud_uri(self) -> None:
        fake_sandbox = SimpleNamespace(
            models=SimpleNamespace(CreateSandboxReqImageInput=Request)
        )
        image = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._create_image_input(
            fake_sandbox,
            "harbor-sandbox.example/seta/973@sha256:" + "a" * 64,
        )
        self.assertTrue(image.Uri.startswith("harbor-sandbox.example/"))
        self.assertFalse(hasattr(image, "Ref"))

    def test_tag_only_image_is_sent_via_yicloud_ref(self) -> None:
        fake_sandbox = SimpleNamespace(
            models=SimpleNamespace(CreateSandboxReqImageInput=Request)
        )
        image = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._create_image_input(
            fake_sandbox,
            "ubuntu:24.04",
        )
        self.assertEqual(image.Ref, "ubuntu:24.04")
        self.assertFalse(hasattr(image, "Uri"))

    def test_control_plane_auth_failure_is_retried(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.logger = Mock()

        class TransientAuthError(RuntimeError):
            @property
            def code(self) -> int:
                return 101

        operation = Mock(side_effect=[TransientAuthError("auth"), "created"])
        with patch.object(
            yicloud_execution.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            result = asyncio.run(
                instance._retry_control_plane_auth(
                    "create sandbox",
                    operation,
                    "request",
                )
            )

        self.assertEqual(result, "created")
        self.assertEqual(operation.call_count, 2)
        sleep.assert_awaited_once_with(1)
        instance.logger.warning.assert_called_once()

    def test_wait_until_running_retries_transient_status_error(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        running = SimpleNamespace(
            Status=SimpleNamespace(State="Running", Reason="")
        )
        get_sandbox = Mock(
            side_effect=[RuntimeError("temporary auth failure"), running]
        )
        instance._sandbox_service = SimpleNamespace(
            get_sandbox=get_sandbox,
            models=SimpleNamespace(GetSandboxReq=Request),
        )
        instance._project_name = "test-project"
        instance._sandbox_id = "sbx-test"
        instance._ready_timeout_sec = 30
        instance._status_log_interval_sec = 30
        instance.logger = Mock()
        created = SimpleNamespace(
            Status=SimpleNamespace(State="Pending", Reason="")
        )

        with patch.object(
            yicloud_execution.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            result = asyncio.run(instance._wait_until_running(created))

        self.assertIs(result, running)
        self.assertEqual(get_sandbox.call_count, 2)
        sleep.assert_awaited_once_with(1)
        instance.logger.warning.assert_called_once()

    def test_wait_until_execd_ready_retries_transient_gateway_error(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._ready_timeout_sec = 120
        instance._ping_execd_sync = Mock(
            side_effect=[
                yicloud_execution.requests.ConnectionError(
                    "temporary gateway failure"
                ),
                None,
            ]
        )
        instance.logger = Mock()

        with patch.object(
            yicloud_execution.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            asyncio.run(instance._wait_until_execd_ready())

        self.assertEqual(instance._ping_execd_sync.call_count, 2)
        instance._ping_execd_sync.assert_has_calls([call("", "")] * 2)
        sleep.assert_awaited_once_with(3)
        instance.logger.warning.assert_called_once()

    def test_composite_execd_readiness_uses_service_endpoint(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.command_url = "https://sandbox.example/worker/command"
        runtime.access_token = "worker-token"
        instance._ready_timeout_sec = 120
        instance._ping_execd_sync = Mock()
        instance.logger = Mock()

        asyncio.run(instance._wait_until_execd_ready(runtime))

        instance._ping_execd_sync.assert_called_once_with(
            runtime.command_url,
            runtime.access_token,
        )

    def test_composite_execd_readiness_rejects_missing_command_url(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.access_token = "worker-token"
        instance._ready_timeout_sec = 120
        instance.logger = Mock()

        with self.assertRaisesRegex(
            RuntimeError,
            "service 'worker' returned no command URL",
        ):
            asyncio.run(instance._wait_until_execd_ready(runtime))

    def test_execd_readiness_uses_configured_ready_timeout(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "sandbox-token"
        instance._ready_timeout_sec = 7
        instance._ping_execd_sync = Mock(
            side_effect=yicloud_execution.requests.ConnectionError(
                "temporary gateway failure"
            )
        )
        instance.logger = Mock()
        fake_time = SimpleNamespace(
            monotonic=Mock(side_effect=[10, 10, 18])
        )

        with (
            patch.object(yicloud_services, "time", fake_time),
            patch.object(
                yicloud_execution.asyncio,
                "sleep",
                new=AsyncMock(),
            ),
            self.assertRaisesRegex(RuntimeError, "within 7s"),
        ):
            asyncio.run(instance._wait_until_execd_ready())

    def test_proxy_origin_can_be_replaced_without_changing_instance_path(self) -> None:
        proxy_url = (
            "https://sandbox.yicloud.com.cn/v1/sandboxes/sbx-1/proxy/44772/ping"
        )
        with patch.dict(
            "os.environ",
            {
                "YICLOUD_SANDBOX_PROXY_ORIGIN": (
                    "https://gate.yicloud.com.cn/sandbox-connect"
                )
            },
        ):
            result = yicloud_client._host_routable_proxy_url(proxy_url)

        self.assertEqual(
            result,
            "https://gate.yicloud.com.cn/sandbox-connect/v1/sandboxes/"
            "sbx-1/proxy/44772/ping",
        )

    def test_proxy_url_is_unchanged_without_an_origin_override(self) -> None:
        proxy_url = "https://sandbox.example/v1/sandboxes/sbx-1/ping"
        with patch.dict("os.environ", {}, clear=True):
            result = yicloud_client._host_routable_proxy_url(proxy_url)
        self.assertEqual(result, proxy_url)

    def test_s3_download_url_is_passed_as_environment_not_command_text(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = True
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )
        artifact = yicloud_transfer.S3UploadArtifact(
            kind="file",
            logical_digest="a" * 64,
            payload_digest="b" * 64,
            payload_size=12,
            compression="none",
            local_payload_path="/cache/payload",
            object_key="objects/payload",
            object_uri="s3://cache/objects/payload",
            download_url="http://ceph.example/cache/object",
        )

        asyncio.run(
            instance._materialize_s3_file(
                artifact,
                "/opt/agent/agent.tgz",
                "755",
            )
        )

        call = instance.exec.await_args
        self.assertNotIn("http://ceph.example", call.args[0])
        self.assertIn("/tmp/harbor-s3-file-", call.args[0])
        self.assertNotIn("/opt/agent/agent.tgz.harbor-upload-", call.args[0])
        self.assertIn("for harbor_attempt in 1 2 3", call.args[0])
        self.assertIn("chmod 755", call.args[0])
        self.assertLess(
            call.args[0].index("actual_digest="),
            call.args[0].index("mkdir -p /opt/agent"),
        )
        self.assertEqual(
            call.kwargs["env"]["HARBOR_S3_URL"],
            artifact.download_url,
        )

    def test_auto_file_upload_falls_back_to_existing_http_transport(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "auto"
        instance._s3_upload_store = SimpleNamespace(
            stage_file=Mock(side_effect=RuntimeError("S3 unavailable"))
        )
        instance._upload_file_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent")
            source.chmod(0o640)
            asyncio.run(instance.upload_file(source, "/opt/agent.tgz"))

        instance._upload_file_http.assert_awaited_once_with(
            source, "/opt/agent.tgz", "640"
        )
        instance.logger.warning.assert_called_once_with(
            "YiCloud upload fallback backend=auto "
            "attempted_backend=s3 fallback_backend=http kind=file "
            "phase=%s target=%s error=%s",
            "stage",
            "/opt/agent.tgz",
            instance._s3_upload_store.stage_file.side_effect,
        )

    def test_auto_file_upload_warns_and_falls_back_after_s3_setup_failure(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "auto"
        instance._s3_upload_store = None
        instance._s3_upload_setup_error = ValueError("invalid S3 profile")
        instance._upload_file_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent")
            source.chmod(0o640)
            asyncio.run(instance.upload_file(source, "/opt/agent.tgz"))

        instance._upload_file_http.assert_awaited_once_with(
            source, "/opt/agent.tgz", "640"
        )
        warning = instance.logger.warning.call_args
        self.assertEqual(
            warning.args[:3],
            (
                (
                    "YiCloud upload fallback backend=auto "
                    "attempted_backend=s3 fallback_backend=http kind=file "
                    "phase=%s target=%s error=%s"
                ),
                "stage",
                "/opt/agent.tgz",
            ),
        )
        self.assertIn(
            "YiCloud S3 upload backend setup failed: invalid S3 profile",
            str(warning.args[3]),
        )

    def test_strict_s3_file_upload_does_not_hide_transport_failure(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "s3"
        instance._s3_upload_store = SimpleNamespace(
            stage_file=Mock(side_effect=RuntimeError("S3 unavailable"))
        )
        instance._upload_file_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent")
            with self.assertRaisesRegex(RuntimeError, "S3 unavailable"):
                asyncio.run(instance.upload_file(source, "/opt/agent.tgz"))

        instance._upload_file_http.assert_not_awaited()

    def test_auto_file_upload_falls_back_when_sandbox_cannot_read_s3(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        artifact = yicloud_transfer.S3UploadArtifact(
            kind="file",
            logical_digest="a" * 64,
            payload_digest="b" * 64,
            payload_size=5,
            compression="none",
            local_payload_path="/cache/payload",
            object_key="objects/payload",
            object_uri="s3://cache/objects/payload",
            download_url="http://ceph.example/cache/object",
        )
        instance._upload_backend = "auto"
        instance._s3_upload_store = SimpleNamespace(
            stage_file=Mock(return_value=artifact)
        )
        instance._materialize_s3_file = AsyncMock(
            side_effect=RuntimeError("anonymous read unavailable")
        )
        instance._upload_file_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent")
            source.chmod(0o644)
            asyncio.run(instance.upload_file(source, "/opt/agent.tgz"))

        instance._materialize_s3_file.assert_awaited_once_with(
            artifact, "/opt/agent.tgz", "644"
        )
        instance._upload_file_http.assert_awaited_once_with(
            source, "/opt/agent.tgz", "644"
        )
        instance.logger.warning.assert_called_once_with(
            "YiCloud upload fallback backend=auto "
            "attempted_backend=s3 fallback_backend=http kind=file "
            "phase=%s target=%s error=%s",
            "materialize",
            "/opt/agent.tgz",
            instance._materialize_s3_file.side_effect,
        )

    def test_auto_directory_upload_falls_back_to_existing_http_transport(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "auto"
        instance._s3_upload_store = SimpleNamespace(
            stage_directory=Mock(side_effect=RuntimeError("S3 unavailable"))
        )
        instance._upload_dir_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "deps"
            source.mkdir()
            (source / "dep.whl").write_bytes(b"wheel")
            asyncio.run(instance.upload_dir(source, "/opt/deps"))

        instance._upload_dir_http.assert_awaited_once_with(source, "/opt/deps")
        instance.logger.warning.assert_called_once_with(
            "YiCloud upload fallback backend=auto "
            "attempted_backend=s3 fallback_backend=http "
            "kind=directory phase=%s target=%s error=%s",
            "stage",
            "/opt/deps",
            instance._s3_upload_store.stage_directory.side_effect,
        )

    def test_oracle_solution_can_bypass_s3_with_explicit_execd_backend(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "s3"
        instance._oracle_solution_upload_backend = "execd"
        instance._s3_upload_store = SimpleNamespace(
            stage_directory=Mock(side_effect=AssertionError("S3 must not be used"))
        )
        instance._upload_dir_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "solution"
            source.mkdir()
            (source / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            asyncio.run(instance.upload_dir(source, "/solution"))

        instance._upload_dir_http.assert_awaited_once_with(source, "/solution")
        instance._s3_upload_store.stage_directory.assert_not_called()
        log_call = instance.logger.info.call_args
        self.assertEqual(
            log_call.args[:2],
            (
                (
                    "YiCloud upload complete backend=execd kind=directory "
                    "purpose=oracle-solution target=%s elapsed_seconds=%.3f"
                ),
                "/solution",
            ),
        )
        self.assertGreaterEqual(log_call.args[2], 0.001)

    def test_oracle_solution_can_be_staged_for_inline_launch(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "s3"
        instance._oracle_solution_upload_backend = "inline"
        instance._oracle_inline_max_bytes = 256 * 1024
        instance._s3_upload_store = SimpleNamespace(
            stage_directory=Mock(side_effect=AssertionError("S3 must not be used"))
        )
        instance._upload_dir_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "solution"
            source.mkdir()
            solve = source / "solve.sh"
            solve.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            solve.chmod(0o755)
            asyncio.run(instance.upload_dir(source, "/solution"))

        payload = base64.b64decode(instance._oracle_inline_solution_payload)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.extractfile("./solve.sh")
            self.assertIsNotNone(member)
            self.assertEqual(member.read(), b"#!/bin/sh\necho ok\n")
        instance._s3_upload_store.stage_directory.assert_not_called()
        instance._upload_dir_http.assert_not_awaited()
        self.assertEqual(instance._oracle_inline_solution_file_count, 1)
        self.assertEqual(instance._oracle_inline_solution_logical_bytes, 18)

    def test_oracle_agent_log_reupload_can_be_skipped_explicitly(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._upload_backend = "s3"
        instance._oracle_skip_agent_log_reupload = True
        instance._s3_upload_store = SimpleNamespace(
            stage_directory=Mock(side_effect=AssertionError("S3 must not be used"))
        )
        instance._upload_dir_http = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent"
            source.mkdir()
            (source / "oracle.txt").write_text("done\n", encoding="utf-8")
            asyncio.run(instance.upload_dir(source, "/logs/agent"))

        instance._s3_upload_store.stage_directory.assert_not_called()
        instance._upload_dir_http.assert_not_awaited()
        instance.logger.info.assert_called_once_with(
            "YiCloud upload skipped purpose=oracle-agent-log-reupload "
            "target=%s",
            "/logs/agent",
        )

    def test_oracle_agent_log_downloads_can_be_skipped_explicitly(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._oracle_skip_agent_log_transfer = True
        instance._download_file_chunks = AsyncMock()
        instance._download_dir_archive = AsyncMock()
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(
                instance.download_file(
                    "/logs/agent/oracle.txt",
                    Path(tmp) / "oracle.txt",
                )
            )
            asyncio.run(instance.download_dir("/logs/agent", Path(tmp) / "agent"))

        instance._download_file_chunks.assert_not_awaited()
        instance._download_dir_archive.assert_not_awaited()
        self.assertEqual(instance.logger.info.call_count, 2)

    def test_s3_bootstrap_is_uploaded_once_only_when_native_tools_are_missing(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = False
        instance._s3_downloader_lock = None
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._access_token = "sandbox-token"
        instance.logger = Mock()
        instance.exec = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    return_code=0, stdout="bootstrap", stderr=""
                ),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        uploaded = {}

        async def capture_upload(source, target_path, upload_url, mode):
            uploaded["count"] = uploaded.get("count", 0) + 1
            uploaded["payload"] = source.read_bytes()
            uploaded["target_path"] = target_path
            uploaded["upload_url"] = upload_url
            uploaded["mode"] = mode

        instance._upload_file_fast = AsyncMock(side_effect=capture_upload)

        async def ensure_twice() -> None:
            download_url = "http://ceph.example/cache/object"
            await instance._ensure_s3_downloader(download_url)
            await instance._ensure_s3_downloader(download_url)

        with patch.dict(
            yicloud_adapter.os.environ,
            {"YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": ""},
        ):
            asyncio.run(ensure_twice())

        self.assertEqual(uploaded["count"], 1)
        self.assertEqual(
            uploaded["target_path"],
            yicloud_transfer.S3_HTTP_BOOTSTRAP_PATH,
        )
        self.assertEqual(
            uploaded["upload_url"],
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/files/upload",
        )
        self.assertEqual(uploaded["mode"], "700")
        self.assertIn(b"/dev/tcp/", uploaded["payload"])
        self.assertLess(len(uploaded["payload"]), 2048)
        self.assertIn(
            yicloud_transfer.S3_HTTP_BOOTSTRAP_PATH,
            instance._s3_download_command(
                SimpleNamespace(payload_size=12, payload_digest="b" * 64),
                "/tmp/payload",
            ),
        )

    def test_environment_and_image_bindings_are_enforced(self) -> None:
        sandbox = FakeSandbox(
            [
                SimpleNamespace(Id="env-other", Name="other"),
                SimpleNamespace(
                    Id="env-dedicated",
                    Name="dedicated-test-environment",
                ),
            ]
        )
        environment_id = yicloud_client._environment_id_by_exact_name(
            sandbox,
            "test-project",
            "dedicated-test-environment",
        )
        self.assertEqual(environment_id, "env-dedicated")

        running = SimpleNamespace(
            EnvironmentId=environment_id,
            Image=SimpleNamespace(Ref="project/task:image"),
        )
        yicloud_client._validate_sandbox_binding(
            running,
            "env-dedicated",
            "project/task:image",
        )
        running.EnvironmentId = "env-other"
        with self.assertRaisesRegex(RuntimeError, "environment binding mismatch"):
            yicloud_client._validate_sandbox_binding(
                running,
                "env-dedicated",
                "project/task:image",
            )

        running.EnvironmentId = "env-dedicated"
        running.Image = SimpleNamespace(
            Ref="", Uri="harbor.example/project/task:image"
        )
        yicloud_client._validate_sandbox_binding(
            running,
            "env-dedicated",
            "harbor.example/project/task:image",
        )
        running.Image = SimpleNamespace(Ref="project/task:image", Uri="")
        yicloud_client._validate_sandbox_binding(
            running,
            "env-dedicated",
            "harbor.example/project/task:image",
        )
        running.Image = SimpleNamespace(
            Ref="", Uri="registry-a.example/project/task:image"
        )
        with self.assertRaisesRegex(RuntimeError, "image binding mismatch"):
            yicloud_client._validate_sandbox_binding(
                running,
                "env-dedicated",
                "registry-b.example/project/task:image",
            )
        running.Image = SimpleNamespace(Ref="ubuntu:22.04", Uri="")
        with self.assertRaisesRegex(RuntimeError, "image binding mismatch"):
            yicloud_client._validate_sandbox_binding(
                running,
                "env-dedicated",
                "ubuntu:24.04",
            )

    def test_tag_only_image_aliases_do_not_contain_empty_ref(self) -> None:
        aliases = yicloud_client._image_ref_aliases("ubuntu:24.04")

        self.assertEqual(aliases, {"ubuntu:24.04"})
        self.assertFalse(
            aliases.intersection(
                yicloud_client._image_ref_aliases("ubuntu:22.04")
            )
        )

    def test_root_exec_payload_uses_uid_zero(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class StopAfterCapture(RuntimeError):
            pass

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                raise StopAfterCapture

        with (
            patch.object(
                yicloud_execution.requests,
                "Session",
                return_value=FakeSession(),
            ),
            self.assertRaises(StopAfterCapture),
        ):
            instance._run_command_sync(
                "id -u",
                "/",
                {},
                30,
                uid=instance._resolve_exec_uid("root"),
            )

        self.assertEqual(captured["payload"]["uid"], 0)

    def test_wrapped_command_preserves_exit_code_after_exit_or_exec(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})

        class StopAfterCapture(RuntimeError):
            pass

        for command, expected_code in (
            ("exit 0", 0),
            ("exit 7", 7),
            ("exec sh -c 'exit 9'", 9),
        ):
            with self.subTest(command=command):
                captured = {}

                class FakeSession:
                    trust_env = True

                    def __init__(self, request_capture):
                        self._request_capture = request_capture

                    def post(self, _url, *, headers, data, timeout):
                        self._request_capture["payload"] = json.loads(data)
                        raise StopAfterCapture

                with (
                    patch.object(
                        yicloud_execution.requests,
                        "Session",
                        return_value=FakeSession(captured),
                    ),
                    self.assertRaises(StopAfterCapture),
                ):
                    instance._run_command_sync(command, "/", {}, 30)

                completed = subprocess.run(
                    ["sh", "-c", captured["payload"]["command"]],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected_code)
                self.assertIn(
                    f"{yicloud_execution.EXIT_MARKER}{expected_code}",
                    completed.stdout,
                )

    def test_exec_retries_chunked_response_with_same_idempotent_command(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        instance.logger = Mock()
        instance._cleanup_remote_exec_paths_sync = Mock()
        bodies = []
        timeouts = []

        class InterruptedResponse:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @property
            def text(self):
                raise yicloud_execution.requests.exceptions.ChunkedEncodingError(
                    "response ended prematurely"
                )

        class SuccessResponse:
            text = (
                '{"type":"stdout","text":"ok"}\n'
                '{"type":"stdout","text":"'
                f"{yicloud_execution.EXIT_MARKER}0"
                '"}\n'
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

        class FakeSession:
            trust_env = True

            def __init__(self, response):
                self._response = response

            def post(self, _url, *, headers, data, timeout):
                bodies.append(data)
                timeouts.append(timeout)
                return self._response

        with (
            patch.object(
                yicloud_execution.requests,
                "Session",
                side_effect=[
                    FakeSession(InterruptedResponse()),
                    FakeSession(SuccessResponse()),
                ],
            ),
            patch.object(yicloud_execution.time, "sleep") as sleep,
        ):
            result = instance._run_command_sync("echo ok", "/", {}, 30)

        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(timeouts, [90, 90])
        self.assertIn("flock -x 9", json.loads(bodies[0])["command"])
        self.assertIn(
            "command -v flock",
            json.loads(bodies[0])["command"],
        )
        cleanup_paths = instance._cleanup_remote_exec_paths_sync.call_args.args
        self.assertTrue(any(path.endswith(".lock") for path in cleanup_paths))
        self.assertTrue(any(path.endswith(".status") for path in cleanup_paths))
        sleep.assert_called_once_with(1)

    def test_wrapped_command_falls_back_when_flock_is_missing(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class StopAfterCapture(RuntimeError):
            pass

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                raise StopAfterCapture

        with (
            patch.object(
                yicloud_execution.requests,
                "Session",
                return_value=FakeSession(),
            ),
            self.assertRaises(StopAfterCapture),
        ):
            instance._run_command_sync("printf fallback", "/", {}, 30)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("cat", "mkdir", "mv", "rm", "sleep"):
                target = shutil.which(name)
                self.assertIsNotNone(target)
                os.symlink(target, bin_dir / name)
            wrapped = re.sub(
                r"/tmp/harbor-execd-[0-9a-f]+",
                str(root / "exec"),
                captured["payload"]["command"],
            )
            result = subprocess.run(
                ["/bin/sh", "-c", wrapped],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PATH": str(bin_dir)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallback", result.stdout)
        self.assertIn(f"{yicloud_execution.EXIT_MARKER}0", result.stdout)

    def test_remote_exec_cleanup_uses_unwrapped_root_request(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        instance.logger = Mock()
        captured = {}

        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                captured["timeout"] = timeout
                return Response()

        with patch.object(
            yicloud_execution.requests,
            "Session",
            return_value=FakeSession(),
        ):
            instance._cleanup_remote_exec_paths_sync("/tmp/a", "/tmp/b")

        self.assertEqual(captured["payload"]["command"], "rm -rf /tmp/a /tmp/b")
        self.assertEqual(captured["payload"]["uid"], 0)
        self.assertNotIn("flock", captured["payload"]["command"])
        self.assertEqual(captured["timeout"], 30)

    def test_long_exec_launches_detached_and_polls_for_result(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(
                    return_code=0,
                    stdout=yicloud_execution.DETACHED_PENDING_MARKER,
                    stderr="",
                ),
                SimpleNamespace(return_code=7, stdout="done", stderr="warn"),
            ]
        )
        instance._cleanup_remote_exec_paths_sync = Mock()

        with patch.object(yicloud_execution.time, "sleep") as sleep:
            result = instance._run_command_sync(
                "sleep 10; exit 7",
                "/work",
                {"A": "B"},
                600,
                1234,
            )

        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, "done")
        self.assertEqual(result.stderr, "warn")
        calls = instance._run_command_direct_sync.call_args_list
        self.assertEqual(len(calls), 3)
        launch_command = calls[0].args[0]
        self.assertIn("nohup", launch_command)
        self.assertIn("command -v bash", launch_command)
        self.assertIn("harbor_detached_started()", launch_command)
        self.assertIn("harbor_launch_dir=", launch_command)
        self.assertLess(
            launch_command.index("harbor_detached_started()"),
            launch_command.index("nohup"),
        )
        self.assertEqual(launch_command.count("9>&-"), 2)
        encoded_script = re.search(
            r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d",
            launch_command,
        )
        self.assertIsNotNone(encoded_script)
        child_script = base64.b64decode(encoded_script.group(1)).decode()
        self.assertIn('printf \'%s\\n\' "$$"', child_script)
        self.assertIn(".pid.tmp", child_script)
        self.assertEqual(calls[0].args[1:], ("/work", {"A": "B"}, 60, 1234))
        self.assertIn("kill -0", calls[1].args[0])
        cleanup_paths = instance._cleanup_remote_exec_paths_sync.call_args.args
        self.assertTrue(any(path.endswith(".sh") for path in cleanup_paths))
        self.assertTrue(any(path.endswith(".pid") for path in cleanup_paths))
        self.assertTrue(any(path.endswith(".pid.tmp") for path in cleanup_paths))
        self.assertTrue(any(path.endswith(".launch") for path in cleanup_paths))
        sleep.assert_called_once()

    def test_oracle_timing_forces_remote_start_marker_and_logs_latency(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(return_code=0, stdout="done", stderr=""),
            ]
        )
        instance._cleanup_remote_exec_paths_sync = Mock()
        instance._create_request_started_at = 10.0
        instance._last_execd_response_received_at = 10.375
        instance._sandbox_id = "sbx-test"
        instance.logger = Mock()

        with (
            patch.dict(os.environ, YICLOUD_SANDBOX_ORACLE_TIMING="1"),
            patch.object(
                yicloud_execution.time,
                "monotonic",
                side_effect=[10.375, 20.0, 19.0],
            ),
        ):
            result = instance._run_command_sync(
                "(/solution/solve.sh) > /logs/agent/oracle.txt 2>&1",
                "/app",
                {},
                60,
                0,
            )

        self.assertEqual(result.stdout, "done")
        launch_command = instance._run_command_direct_sync.call_args_list[0].args[0]
        encoded_script = re.search(
            r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d",
            launch_command,
        )
        self.assertIsNotNone(encoded_script)
        child_script = base64.b64decode(encoded_script.group(1)).decode()
        self.assertIn(".started.tmp", child_script)
        self.assertIn('harbor_started_path=', launch_command)
        self.assertIn('[ -f "$harbor_started_path" ]', launch_command)
        instance.logger.info.assert_called_once_with(
            "YiCloud Oracle process started sandbox_id=%s "
            "create_to_oracle_start_ms=%.3f "
            "observation=remote_pre_exec_marker "
            "create_to_oracle_start_lower_bound_ms=%.3f "
            "confirmation_window_ms=%.3f "
            "ready_to_oracle_start_lower_bound_ms=%s "
            "ready_to_oracle_start_ms=%s "
            "initialized_to_oracle_start_lower_bound_ms=%s "
            "remote_uptime_seconds=%s",
            "sbx-test",
            375.0,
            375.0,
            0.0,
            "unknown",
            "unknown",
            "unknown",
            "unknown",
        )

    def test_oracle_timing_does_not_treat_chmod_as_solution_start(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        expected = SimpleNamespace(return_code=0, stdout="", stderr="")
        instance._run_command_direct_sync = Mock(return_value=expected)
        instance._run_command_detached_sync = Mock()

        with patch.dict(os.environ, YICLOUD_SANDBOX_ORACLE_TIMING="1"):
            result = instance._run_command_sync(
                "chmod +x /solution/solve.sh",
                "/app",
                {},
                None,
                0,
            )

        self.assertIs(result, expected)
        instance._run_command_detached_sync.assert_not_called()

    def test_oracle_inline_chmod_is_deferred_into_launch_request(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._oracle_inline_solution_payload = "payload"
        instance._sandbox_id = "sbx-test"
        instance.logger = Mock()
        instance._run_command_direct_sync = Mock()

        result = instance._run_command_sync(
            "chmod +x /solution/solve.sh",
            "/app",
            {},
            None,
            0,
        )

        self.assertEqual(result.return_code, 0)
        self.assertEqual(
            instance._oracle_inline_chmod_command,
            "chmod +x /solution/solve.sh",
        )
        instance._run_command_direct_sync.assert_not_called()

    def test_oracle_inline_launch_reports_remote_payload_and_solve_timing(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._oracle_inline_solution_payload = "cGF5bG9hZA=="
        instance._oracle_inline_solution_archive_bytes = 7
        instance._oracle_inline_solution_logical_bytes = 7
        instance._oracle_inline_solution_file_count = 1
        instance._oracle_inline_chmod_command = "chmod +x /solution/solve.sh"
        instance._create_request_started_at = 10.0
        instance._ready_observed_at = 10.2
        instance._initialized_at = 10.3
        instance._last_execd_response_received_at = 10.6
        instance._sandbox_id = "sbx-test"
        instance.logger = Mock()
        instance._cleanup_remote_exec_paths_sync = Mock()
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(
                    return_code=0,
                    stdout=(
                        f"{yicloud_execution.ORACLE_REMOTE_START_MARKER}123.00\n"
                        f"{yicloud_execution.ORACLE_PAYLOAD_TIMING_MARKER}"
                        "122.98 123.00\n"
                    ),
                    stderr="",
                ),
                SimpleNamespace(
                    return_code=0,
                    stdout=(
                        f"{yicloud_execution.ORACLE_REMOTE_TIMING_MARKER}"
                        "123.00 123.05 \ndone"
                    ),
                    stderr="",
                ),
            ]
        )

        with (
            patch.dict(os.environ, YICLOUD_SANDBOX_ORACLE_TIMING="1"),
            patch.object(
                yicloud_execution.time,
                "monotonic",
                side_effect=[10.5, 20.0, 19.0],
            ),
        ):
            result = instance._run_command_sync(
                "(/solution/solve.sh) > /logs/agent/oracle.txt 2>&1",
                "/app",
                {},
                60,
                0,
            )

        launch_command = instance._run_command_direct_sync.call_args_list[0].args[0]
        self.assertIn("tar xzf - -C /solution", launch_command)
        self.assertIn("chmod +x /solution/solve.sh", launch_command)
        self.assertEqual(result.stdout, "done")
        messages = [call.args[0] for call in instance.logger.info.call_args_list]
        self.assertTrue(any("payload materialized" in message for message in messages))
        self.assertTrue(any("process finished" in message for message in messages))

    def test_detached_timeout_kills_process_tree_without_setsid(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        instance._cleanup_remote_exec_paths_sync = Mock()

        with patch.object(
            yicloud_execution.time,
            "monotonic",
            side_effect=[0, 2],
        ):
            result = instance._run_command_detached_sync(
                "sleep 60",
                "/work",
                {},
                1,
                1234,
            )

        self.assertEqual(result.return_code, 124)
        terminate_command = instance._run_command_direct_sync.call_args_list[1].args[0]
        self.assertIn('kill -TERM -- "-$harbor_pid"', terminate_command)
        self.assertIn('kill -KILL -- "-$harbor_pid"', terminate_command)
        self.assertIn("for harbor_status in /proc/[0-9]*/status", terminate_command)
        self.assertIn('"$harbor_key" = PPid', terminate_command)
        self.assertIn('kill -TERM "$harbor_tree_pid"', terminate_command)
        self.assertIn('kill -KILL "$harbor_tree_pid"', terminate_command)
        instance._cleanup_remote_exec_paths_sync.assert_called_once()

    @unittest.skipUnless(
        Path("/proc/self/task").is_dir() and shutil.which("setsid"),
        "requires Linux procfs and setsid",
    )
    def test_detached_launch_command_is_idempotent(self) -> None:
        command_id = f"launch-once-{os.getpid()}"
        count_path = Path(f"/tmp/{command_id}.count")
        prefix = f"/tmp/harbor-detached-{command_id}"
        pid_path = Path(f"{prefix}.pid")
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        instance._cleanup_remote_exec_paths_sync = Mock()

        with (
            patch.object(
                yicloud_execution.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex=command_id),
            ),
            patch.object(
                yicloud_execution.time,
                "monotonic",
                side_effect=[0, 2],
            ),
        ):
            instance._run_command_detached_sync(
                f"printf 'started\\n' >> {shlex.quote(str(count_path))}; "
                "sleep 300",
                "/",
                {},
                1,
                0,
            )

        calls = instance._run_command_direct_sync.call_args_list
        launch_command = calls[0].args[0]
        terminate_command = calls[1].args[0]
        detached_pid: int | None = None
        try:
            for _ in range(2):
                launched = subprocess.run(
                    ["/bin/sh", "-c", launch_command],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(launched.returncode, 0, launched.stderr)

            deadline = time.monotonic() + 5
            while not count_path.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(
                count_path.read_text(encoding="utf-8").splitlines(),
                ["started"],
            )
            detached_pid = int(pid_path.read_text(encoding="utf-8"))
            self.assertEqual(os.getpgid(detached_pid), detached_pid)
            terminated = subprocess.run(
                ["/bin/sh", "-c", terminate_command],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(terminated.returncode, 0, terminated.stderr)
        finally:
            if detached_pid is not None:
                try:
                    os.killpg(detached_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            count_path.unlink(missing_ok=True)
            for path in Path("/tmp").glob(
                f"harbor-detached-{command_id}*"
            ):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

    @unittest.skipUnless(
        Path("/proc/self/task").is_dir(),
        "requires Linux procfs",
    )
    def test_detached_timeout_process_tree_fallback_stops_descendants(
        self,
    ) -> None:
        command_id = f"tree-test-{os.getpid()}"
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        instance._cleanup_remote_exec_paths_sync = Mock()

        with (
            patch.object(
                yicloud_execution.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex=command_id),
            ),
            patch.object(
                yicloud_execution.time,
                "monotonic",
                side_effect=[0, 2],
            ),
        ):
            instance._run_command_detached_sync(
                "sleep 60",
                "/work",
                {},
                1,
                1234,
            )

        terminate_command = instance._run_command_direct_sync.call_args_list[1].args[0]
        pid_path = Path(f"/tmp/harbor-detached-{command_id}.pid")
        child_pid_path = Path(f"/tmp/harbor-detached-{command_id}.child")
        parent_command = (
            "import subprocess; "
            "child = subprocess.Popen(['sleep', '300']); "
            f"open({str(child_pid_path)!r}, 'w').write(str(child.pid)); "
            "child.wait()"
        )
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                parent_command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid: int | None = None
        try:
            self.assertNotEqual(os.getpgid(parent.pid), parent.pid)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child_pid_path.is_file():
                    child_pid = int(
                        child_pid_path.read_text(encoding="utf-8")
                    )
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid)
            pid_path.write_text(f"{parent.pid}\n", encoding="utf-8")

            terminated = subprocess.run(
                ["/bin/sh", "-c", terminate_command],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(terminated.returncode, 0, terminated.stderr)
            parent.wait(timeout=5)
            child_path = Path(f"/proc/{child_pid}")
            deadline = time.monotonic() + 5
            while child_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(child_path.exists())
        finally:
            pid_path.unlink(missing_ok=True)
            child_pid_path.unlink(missing_ok=True)
            for pid in (child_pid, parent.pid):
                if pid is None:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if parent.poll() is None:
                parent.wait(timeout=5)

    def test_exec_without_explicit_timeout_stays_direct(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        expected = SimpleNamespace(return_code=0, stdout="ok", stderr="")
        instance._run_command_direct_sync = Mock(return_value=expected)
        instance._run_command_detached_sync = Mock()

        result = instance._run_command_sync("echo ok", "/", {}, None, 0)

        self.assertIs(result, expected)
        instance._run_command_direct_sync.assert_called_once_with(
            "echo ok", "/", {}, None, 0
        )
        instance._run_command_detached_sync.assert_not_called()

    def test_exec_uses_harbor_default_user_when_user_is_unset(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.default_user = "1234"
        instance.task_env_config = SimpleNamespace(workdir="/app")
        instance._merge_env = Mock(return_value={})
        instance._run_command_sync = Mock(
            return_value=SimpleNamespace(
                stdout="",
                stderr="",
                return_code=0,
            )
        )
        instance._output_callback = Mock(return_value=None)

        async def run_inline(function, *args):
            return function(*args)

        with patch.object(
            yicloud_execution.asyncio,
            "to_thread",
            side_effect=run_inline,
        ):
            asyncio.run(instance.exec("id -u"))

        self.assertEqual(instance._run_command_sync.call_args.args[1], "/app")
        self.assertEqual(instance._run_command_sync.call_args.args[-1], 1234)

    def test_exec_omits_cwd_when_no_cwd_or_task_workdir_is_set(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.default_user = None
        instance.task_env_config = SimpleNamespace(workdir=None)
        instance._merge_env = Mock(return_value={})
        instance._output_callback = Mock(return_value=None)
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class FakeResponse:
            text = ""

            @staticmethod
            def raise_for_status() -> None:
                return None

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                return FakeResponse()

        async def run_inline(function, *args):
            return function(*args)

        with (
            patch.object(
                yicloud_execution.requests,
                "Session",
                return_value=FakeSession(),
            ),
            patch.object(
                yicloud_execution.asyncio,
                "to_thread",
                side_effect=run_inline,
            ),
        ):
            result = asyncio.run(instance.exec("pwd"))

        self.assertNotIn("cwd", captured["payload"])
        self.assertEqual(result.return_code, 1)

    def test_fast_upload_keeps_access_token_out_of_argv(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._access_token = "secret-sandbox-token"
        instance.logger = Mock()
        captured = {}

        def fake_run(command, **_kwargs):
            captured["command"] = command
            header_path = Path(command[command.index("--header") + 1][1:])
            captured["headers"] = header_path.read_text(encoding="utf-8")
            metadata_form = next(
                value
                for value in command
                if value.startswith("metadata=@")
            )
            metadata_path = Path(
                metadata_form.removeprefix("metadata=@").split(";", 1)[0]
            )
            captured["metadata"] = json.loads(metadata_path.read_text())
            return SimpleNamespace(returncode=0, stdout="200", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent-package")
            source.chmod(0o755)
            with patch.object(
                yicloud_transfer.subprocess,
                "run",
                side_effect=fake_run,
            ):
                instance._upload_file_fast_sync(
                    source,
                    "/opt/tb-opik/agent.tgz",
                    instance._fast_upload_url(),
                )

        self.assertNotIn(
            "secret-sandbox-token",
            " ".join(captured["command"]),
        )
        self.assertIn(
            "X-Sandbox-Access-Token: secret-sandbox-token",
            captured["headers"],
        )
        self.assertEqual(captured["metadata"]["mode"], 755)

    def test_fast_upload_reuses_host_routable_execd_endpoint(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )

        with patch.dict(
            yicloud_adapter.os.environ,
            {"YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": ""},
        ):
            upload_url = instance._fast_upload_url()

        self.assertEqual(
            upload_url,
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/files/upload",
        )

    def test_fast_upload_failure_without_body_reports_curl_error(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._access_token = "secret-sandbox-token"
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent-package")
            with (
                patch.object(
                    yicloud_transfer.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=28,
                        stdout="000",
                        stderr="connection timed out",
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "connection timed out"),
            ):
                instance._upload_file_fast_sync(
                    source,
                    "/opt/tb-opik/agent.tgz",
                    "https://gate.example/files/upload",
                )

    def test_large_fast_upload_is_chunked_and_reassembled(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._uses_s3_upload = Mock(return_value=False)
        instance._fast_upload_url = Mock(
            return_value="https://gate.example/files/upload"
        )
        uploaded = []

        def capture_upload(content, target_path, _filename):
            uploaded.append((content, target_path))

        instance._upload_chunk_sync = capture_upload
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runtime.tgz"
            source.write_bytes(b"abcdefghi")
            source.chmod(0o755)
            with (
                patch.object(
                    yicloud_transfer, "FAST_UPLOAD_CHUNK_BYTES", 4
                ),
                patch.dict(
                    yicloud_adapter.os.environ,
                    {"YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": ""},
                ),
            ):
                asyncio.run(
                    instance.upload_file(source, "/opt/tools/runtime.tgz")
                )

        self.assertEqual(
            [part[0] for part in uploaded], [b"abcd", b"efgh", b"i"]
        )
        self.assertEqual(len({part[1] for part in uploaded}), 3)
        commands = [call.args[0] for call in instance.exec.await_args_list]
        self.assertTrue(any("base64 -d" in command for command in commands))
        self.assertTrue(any("chmod 755" in command for command in commands))

    def test_chunked_upload_restores_source_mode(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._uses_s3_upload = Mock(return_value=False)
        instance._fast_upload_url = Mock(return_value="")
        instance._upload_chunk_sync = Mock()
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        async def run_inline(function, *args):
            return function(*args)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tool"
            source.write_bytes(b"#!/bin/sh\n")
            source.chmod(0o755)
            with patch.object(
                yicloud_execution.asyncio,
                "to_thread",
                side_effect=run_inline,
            ):
                asyncio.run(instance.upload_file(source, "/opt/tools/tool"))

        commands = [call.args[0] for call in instance.exec.await_args_list]
        self.assertIn("chmod 755 /opt/tools/tool", commands)

    def test_delete_disconnect_does_not_replace_completed_trial(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance._sandbox_name = "trial-test"
        instance._cleanup_wait_sec = 1
        instance.logger = Mock()
        instance._detach_sandbox = Mock()
        sandbox = Mock()
        sandbox.models = SimpleNamespace(
            DeleteSandboxReq=Request,
            ListSandboxesReq=Request,
        )
        sandbox.delete_sandbox.side_effect = ConnectionError(
            "response disconnected"
        )
        # The request can have succeeded even when its response was lost.
        sandbox.list_sandboxes.return_value = SimpleNamespace(Items=[])
        instance._sandbox_service = sandbox

        asyncio.run(instance._delete_sandbox())

        sandbox.delete_sandbox.assert_called_once()
        sandbox.list_sandboxes.assert_called_once()
        instance._detach_sandbox.assert_called_once()
        instance.logger.warning.assert_called_once()

    def test_batch_delete_disconnect_is_confirmed_by_listing(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance.logger = Mock()
        sandbox = Mock()
        sandbox.models = SimpleNamespace(
            BatchDeleteSandboxesReq=Request,
            ListSandboxesReq=Request,
        )
        sandbox.batch_delete_sandboxes.side_effect = ConnectionError(
            "response disconnected"
        )
        sandbox.list_sandboxes.return_value = SimpleNamespace(Items=[])
        instance._sandbox_service = sandbox

        deleted = asyncio.run(
            instance._batch_delete_sandboxes({"sbx-main", "sbx-worker"})
        )

        self.assertTrue(deleted)
        sandbox.batch_delete_sandboxes.assert_called_once()
        sandbox.list_sandboxes.assert_called_once()
        instance.logger.warning.assert_called_once()

    def test_execd_upload_uses_binary_multipart_metadata(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._request_timeout_sec = 180
        instance._signed_headers = Mock(
            return_value={
                "X-OGW-SIGN": "signed",
                "X-Sandbox-Access-Token": "token",
            }
        )
        sent = {}

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        class FakeSession:
            trust_env = True

            def prepare_request(self, request):
                return (
                    yicloud_execution.requests.sessions.Session()
                    .prepare_request(request)
                )

            def send(self, prepared, timeout):
                sent["prepared"] = prepared
                sent["timeout"] = timeout
                return FakeResponse()

        with patch.object(
            yicloud_execution.requests,
            "Session",
            FakeSession,
        ):
            instance._upload_chunk_sync(
                b"\x00\xffagent-package",
                "/tmp/harbor-upload.chunk",
                "agent.tgz",
            )

        prepared = sent["prepared"]
        self.assertTrue(prepared.url.endswith("/files/upload"))
        self.assertIn(
            base64.b64encode(b"\x00\xffagent-package"),
            prepared.body,
        )
        self.assertIn(
            b'name="metadata"; filename="metadata.json"',
            prepared.body,
        )
        self.assertIn(b'"mode":600', prepared.body)
        self.assertEqual(prepared.headers["X-OGW-SIGN"], "signed")

    def test_execd_upload_retries_transient_disconnect(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._request_timeout_sec = 180
        instance._signed_headers = Mock(return_value={})
        instance.logger = Mock()
        sends = []

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        class FakeSession:
            trust_env = True

            def prepare_request(self, request):
                return (
                    yicloud_execution.requests.sessions.Session()
                    .prepare_request(request)
                )

            def send(self, prepared, timeout):
                sends.append((prepared, timeout))
                if len(sends) == 1:
                    raise yicloud_execution.requests.ConnectionError(
                        "response disconnected"
                    )
                return FakeResponse()

        with (
            patch.object(
                yicloud_execution.requests,
                "Session",
                FakeSession,
            ),
            patch.object(yicloud_execution.time, "sleep") as sleep,
        ):
            instance._upload_chunk_sync(
                b"agent-package",
                "/tmp/harbor-upload.chunk",
                "agent.tgz",
            )

        self.assertEqual(len(sends), 2)
        sleep.assert_called_once_with(1)
        instance.logger.warning.assert_called_once()


class BoundedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = patch.dict(os.environ, {
            "HARBOR_ARTIFACT_LOCK_DIR": str(self.root / "locks"),
            "HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY": "2",
        })
        self.settings.start()
        self.instance = object.__new__(yicloud_opensandbox.YiCloudOpenSandboxEnvironment)
        self.instance.logger = Mock()
        self.commands = []
        self.response_sizes = []
        self.instance.exec = self.local_exec

    async def asyncTearDown(self):
        self.settings.stop()
        self.temporary.cleanup()

    async def local_exec(self, command, **kwargs):
        self.commands.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        self.response_sizes.append(len(stdout) + len(stderr))
        return SimpleNamespace(
            return_code=process.returncode,
            stdout=stdout.decode(), stderr=stderr.decode(),
        )

    async def test_large_binary_download_bounds_responses_and_preserves_hash_and_mode(self):
        import hashlib
        source = self.root / "source ' with spaces"
        target = self.root / "target"
        # Incompressible data spanning multiple transfer requests.
        source.write_bytes(os.urandom(3 * yicloud_transfer.DOWNLOAD_CHUNK_BYTES + 37))
        target.write_bytes(b"old")
        target.chmod(0o640)
        await self.instance.download_file(str(source), target)
        with source.open("rb") as left, target.open("rb") as right:
            self.assertEqual(hashlib.file_digest(left, "sha256").digest(),
                             hashlib.file_digest(right, "sha256").digest())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertLessEqual(max(self.response_sizes),
                             4 * ((yicloud_transfer.DOWNLOAD_CHUNK_BYTES + 2) // 3))
        self.assertFalse(list(self.root.glob("*.part")))

    async def test_file_symlink_uses_referent_size(self):
        source = self.root / "source"
        source.write_bytes(b"full referent content")
        link = self.root / "link"
        link.symlink_to("source")
        await self.instance.download_file(str(link), self.root / "out")
        self.assertEqual((self.root / "out").read_bytes(), source.read_bytes())

    async def test_download_metadata_survives_execd_line_separator_loss(self):
        source = self.root / "source"
        source.write_bytes(b"content")

        async def exec_without_newlines(command, **kwargs):
            result = await self.local_exec(command, **kwargs)
            result.stdout = result.stdout.replace("\n", "")
            return result

        self.instance.exec = exec_without_newlines
        await self.instance.download_file(str(source), self.root / "out")
        self.assertEqual((self.root / "out").read_bytes(), source.read_bytes())

    async def test_empty_file(self):
        source = self.root / "empty"
        source.touch()
        await self.instance.download_file(str(source), self.root / "out")
        self.assertEqual((self.root / "out").read_bytes(), b"")
        self.assertFalse(any("dd if=" in command for command in self.commands))

    async def test_corrupt_short_and_changed_content_do_not_replace_target(self):
        source = self.root / "source"
        source.write_bytes(b"original")
        target = self.root / "target"
        for payload, expected in (("not-base64!", ValueError),
                                  (base64.b64encode(b"short").decode(), RuntimeError),
                                  (base64.b64encode(b"modified").decode(), RuntimeError)):
            with self.subTest(payload=payload):
                target.write_bytes(b"previous valid target")

                async def corrupt(command, payload=payload, **kwargs):
                    if "dd if=" in command:
                        return SimpleNamespace(return_code=0, stdout=payload, stderr="")
                    return await self.local_exec(command, **kwargs)

                self.instance.exec = corrupt
                with self.assertRaises(expected):
                    await self.instance.download_file(str(source), target)
                self.assertEqual(target.read_bytes(), b"previous valid target")
                self.assertFalse(list(self.root.glob("*.part")))

    async def test_directory_archive_and_cleanup(self):
        source = self.root / "source dir"
        source.mkdir()
        (source / "file").write_bytes(os.urandom(1024 * 1024))
        os.link(source / "file", source / "hardlink")
        (source / "symlink").symlink_to("file")
        target = self.root / "out"
        await self.instance.download_dir(str(source), target)
        self.assertEqual((source / "file").read_bytes(), (target / "file").read_bytes())
        self.assertTrue((target / "symlink").is_symlink())
        self.assertEqual((target / "file").stat().st_ino, (target / "hardlink").stat().st_ino)
        cleanup = self.commands[-1]
        self.assertTrue(cleanup.startswith("rm -f -- /tmp/harbor-download-"))
        self.assertFalse(Path(shlex.split(cleanup)[-1]).exists())

    async def test_excluded_directory_uses_bounded_download_and_cleanup(self):
        source = self.root / "source"
        source.mkdir()
        (source / "keep").write_text("keep")
        (source / "omit").write_text("omit")
        target = self.root / "out"
        await self.instance.download_dir_with_exclusions(
            source_dir=str(source), target_dir=target, exclude=["omit"],
        )
        self.assertEqual((target / "keep").read_text(), "keep")
        self.assertFalse((target / "omit").exists())
        self.assertTrue(any("dd if=" in command for command in self.commands))
        self.assertTrue(self.commands[-1].startswith("rm -f -- /tmp/harbor-download-"))

    async def test_tar_failure_is_reported_and_remote_archive_cleaned(self):
        with self.assertRaisesRegex(RuntimeError, "failed to archive.*return_code=2") as raised:
            await self.instance.download_dir(str(self.root / "missing"), self.root / "out")
        self.assertIn("missing", str(raised.exception))
        self.assertFalse(any("dd if=" in command for command in self.commands))
        self.assertTrue(self.commands[-1].startswith("rm -f -- /tmp/harbor-download-"))

    async def test_tar_error_preserves_bounded_output_without_accepting_failure(self):
        for stdout, stderr, expected in (
            ("", "x" * 3000 + "tar: missing source", "x" * 1981 + "tar: missing source"),
            ("command unavailable", "", "command unavailable"),
            ("", "", "no output"),
        ):
            with self.subTest(expected=expected[-40:]):
                self.instance.exec = AsyncMock(side_effect=[
                    SimpleNamespace(return_code=1, stdout=stdout, stderr=stderr),
                    SimpleNamespace(return_code=0, stdout="", stderr=""),
                ])
                with patch.object(self.instance, "_download_file_chunks", new_callable=AsyncMock) as download:
                    with self.assertRaises(RuntimeError) as raised:
                        await self.instance.download_dir("/logs/verifier", self.root / "out")
                    self.assertEqual(str(raised.exception),
                                     f"failed to archive directory '/logs/verifier': return_code=1; {expected}")
                    download.assert_not_awaited()
                self.assertTrue(self.instance.exec.await_args_list[-1].args[0].startswith("rm -f -- "))

    async def test_cancelled_transfer_cleans_part_and_remote_archive(self):
        source = self.root / "source"
        source.mkdir()
        (source / "file").write_text("some data")

        async def cancel_read(command, **kwargs):
            if "dd if=" in command:
                raise asyncio.CancelledError
            return await self.local_exec(command, **kwargs)

        self.instance.exec = cancel_read
        with self.assertRaises(asyncio.CancelledError):
            await self.instance.download_dir(str(source), self.root / "out")
        self.assertTrue(self.commands[-1].startswith("rm -f -- /tmp/harbor-download-"))
        self.assertFalse(Path(shlex.split(self.commands[-1])[-1]).exists())

    async def test_unsafe_archive_rejected(self):
        import io
        archive = self.root / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("../escaped")
            member.size = 4
            tar.addfile(member, io.BytesIO(b"oops"))

        async def unsafe_archive(command, **kwargs):
            if command.startswith("tar czf "):
                remote = shlex.split(command)[2]
                shutil.copyfile(archive, remote)
                return SimpleNamespace(return_code=0, stdout="", stderr="")
            return await self.local_exec(command, **kwargs)

        self.instance.exec = unsafe_archive
        with self.assertRaises(tarfile.FilterError):
            await self.instance.download_dir(str(self.root), self.root / "out")
        self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
