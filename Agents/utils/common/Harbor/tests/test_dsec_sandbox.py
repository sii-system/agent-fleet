"""Test DSec adapter lifecycle, idempotent identity, and protocol boundaries."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
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

        def _merge_env(self, env):
            return env

        def _output_callback(self):
            return None

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
adapter = importlib.import_module(
    "harbor_environment_adapters.providers.dsec_controller.adapter"
)
transfer = importlib.import_module(
    "harbor_environment_adapters.providers.dsec_controller.transfer"
)


class DsecLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_passes_transfer_timeout_to_archive_client(self):
        with patch.object(adapter.BaseEnvironment, "__init__", lambda self, **kwargs:
                          setattr(self, "_network_is_public", False)), patch.dict(os.environ, {
            "DSEC_SANDBOX_CONTROLLER_ENDPOINT": "https://controller.example",
            "DSEC_SANDBOX_TRANSFER_TIMEOUT_SEC": "37",
        }):
            environment = adapter.DsecSandboxEnvironment()
            try:
                self.assertEqual(environment._client.transfer_timeout_sec, 37)
                self.assertEqual(environment._client.request_timeout_sec, 15)
            finally:
                environment._client.session.close()

    def environment(self):
        instance = object.__new__(adapter.DsecSandboxEnvironment)
        instance._client = SimpleNamespace(
            create_sandbox=AsyncMock(),
            sandbox_status=AsyncMock(),
            close_sandbox=AsyncMock(),
            create_execution=AsyncMock(),
            cancel_execution=AsyncMock(),
            execution_status=AsyncMock(),
            execution_output=AsyncMock(return_value=b""),
        )
        instance._image_ref_override = (
            "registry.example/tasks/sample@sha256:" + "a" * 64
        )
        instance._bundle_manifest_path = ""
        instance._sandbox_id = ""
        instance._execution_sequence = 0
        instance._ttl_seconds = 3600
        instance._network_mode = "none"
        instance._ready_timeout_sec = 2
        instance._close_timeout_sec = 2
        instance._default_exec_timeout_sec = 2
        instance._exec_output_limit_bytes = 4096
        instance._request_attempts = 3
        instance._poll_interval_sec = 0.001
        instance.session_id = "trial-one__env"
        instance.context_id = None
        instance.default_user = "root"
        instance.task_env_config = SimpleNamespace(
            docker_image=None, workdir="/workspace"
        )
        instance.logger = logging.getLogger("test-dsec")
        instance._persistent_env = {"A": "1"}
        instance._effective_cpus = None
        instance._effective_memory_mb = None
        instance._effective_storage_mb = None
        instance._resource_request = lambda: {
            "policy": "shared/v1",
            "cpu_millicores": 0,
            "memory_bytes": 0,
            "pids_limit": 0,
        }
        instance._validate_definition = lambda: None
        instance._prepare_harbor_directories = AsyncMock()
        instance._upload_environment_dir_after_start = AsyncMock()
        instance._materialize_read_only_mounts = AsyncMock()
        return instance

    async def test_start_waits_ready_initializes_and_uses_stable_spec(self):
        environment = self.environment()
        environment._client.create_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "creating",
        }
        environment._client.sandbox_status.return_value = {
            "sandbox_id": "sbx-one",
            "state": "ready",
            "observation": {"reachable": True},
        }
        environment._client.create_execution.return_value = {
            "execution_id": "exec-one",
            "state": "succeeded",
            "exit_code": 0,
            "stdout": {"committed_bytes": 0, "truncated": False},
            "stderr": {"committed_bytes": 0, "truncated": False},
        }

        await environment.start(force_build=False)

        self.assertEqual(environment._sandbox_id, "sbx-one")
        spec = environment._client.create_sandbox.await_args.args[0]
        self.assertEqual(spec["process"]["user"], "root")
        self.assertEqual(spec["process"]["env"], {"A": "1"})
        self.assertEqual(
            spec["image_delivery"],
            {"mode": "lazy-required", "fallback": "none"},
        )
        self.assertNotIn("network_mode", spec)
        self.assertEqual(spec["request_id"], environment._sandbox_request_id())
        environment._upload_environment_dir_after_start.assert_awaited_once()

    def test_public_network_maps_only_to_explicit_bridge(self):
        environment = self.environment()
        environment._network_mode = "bridge"

        spec = environment._create_spec()

        self.assertEqual(spec["network_mode"], "bridge")
        capabilities = environment.capabilities
        self.assertTrue(capabilities.disable_internet)
        self.assertTrue(capabilities.network_allowlist)
        self.assertTrue(capabilities.dynamic_network_policy)

    async def test_allowlist_transition_warns_and_continues(self):
        environment = self.environment()
        policy = SimpleNamespace(
            network_mode=SimpleNamespace(value="allowlist"),
            allowed_hosts=["model.example"],
        )

        with self.assertLogs("test-dsec", level="WARNING") as captured:
            await environment._apply_network_policy(policy)

        self.assertIn("does not enforce Harbor network allowlists", captured.output[0])

    async def test_stop_closes_even_when_harbor_requests_retention(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "closed",
        }

        await environment.stop(delete=False)

        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")
        self.assertEqual(environment._sandbox_id, "")

    async def test_stop_retains_sandbox_identity_when_close_request_fails(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.close_sandbox.side_effect = RuntimeError("controller unavailable")

        with self.assertRaisesRegex(RuntimeError, "controller unavailable"):
            await environment.stop(delete=True)

        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_stop_retains_sandbox_identity_when_close_poll_fails(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "closing",
        }
        environment._wait_sandbox_state = AsyncMock(
            side_effect=TimeoutError("close timed out")
        )

        with self.assertRaisesRegex(TimeoutError, "close timed out"):
            await environment.stop(delete=True)

        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_exec_preserves_nonzero_exit_and_rejects_user_switch(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-one",
            "state": "failed",
            "exit_code": 7,
            "stdout": {"committed_bytes": 2, "truncated": False},
            "stderr": {"committed_bytes": 0, "truncated": False},
        }
        environment._client.execution_output.return_value = b"ok"

        result = await environment.exec("exit 7", user="root")

        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(
            environment._client.create_execution.await_args.args[1]["argv"],
            ["/bin/bash", "-lc", "exit 7"],
        )
        with self.assertRaisesRegex(ValueError, "root execution identity"):
            await environment.exec("true", user="1000")

    async def test_oversized_command_is_materialized_with_bounded_argv(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        uploaded = b""

        async def capture_upload(source, _target):
            nonlocal uploaded
            uploaded = Path(source).read_bytes()

        environment.upload_file = AsyncMock(side_effect=capture_upload)
        environment._execute_argv = AsyncMock(
            return_value=(
                {
                    "state": "succeeded",
                    "exit_code": 0,
                    "stdout": {"committed_bytes": 0, "truncated": False},
                    "stderr": {"committed_bytes": 0, "truncated": False},
                },
                b"ok",
                b"",
            )
        )
        command = "printf x # " + "x" * 5000

        result = await environment.exec(command)

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(uploaded, command.encode())
        argv = environment._execute_argv.await_args.args[0]
        self.assertEqual(argv[0], "/bin/bash")
        self.assertLessEqual(max(len(item.encode()) for item in argv), 4096)
        self.assertEqual(len(environment._execute_argv.await_args_list), 1)

    async def test_start_failure_shields_close(self):
        environment = self.environment()
        environment._client.create_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "create_failed",
        }
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "create_failed",
        }

        with self.assertRaisesRegex(RuntimeError, "terminal state"):
            await environment.start(force_build=False)
        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")

    async def test_execution_timeout_closes_the_whole_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-one",
            "state": "running",
        }
        environment._client.execution_status.return_value = {
            "execution_id": "exec-one",
            "state": "running",
        }
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "closed",
        }

        with self.assertRaisesRegex(TimeoutError, "exec-one"):
            await environment._execute_argv(["sleep", "60"], timeout_sec=0.002)

        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")
        self.assertEqual(environment._sandbox_id, "")

    async def test_execution_cancellation_closes_the_whole_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        request_started = asyncio.Event()

        async def wait_for_cancellation(*_args):
            request_started.set()
            await asyncio.Event().wait()

        environment._client.create_execution.side_effect = wait_for_cancellation
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "closed",
        }
        task = asyncio.create_task(environment._execute_argv(["sleep", "60"]))
        await request_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")
        self.assertEqual(environment._sandbox_id, "")

    async def test_exec_forwards_priority_and_cancels_queued_orphan_on_non_closing_timeout(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-prio",
            "state": "succeeded",
            "exit_code": 0,
            "stdout": {"committed_bytes": 0, "truncated": False},
            "stderr": {"committed_bytes": 0, "truncated": False},
        }
        await environment.exec("echo hi", priority="completion")
        self.assertEqual(
            environment._client.create_execution.await_args.args[1]["priority"],
            "completion",
        )

        # close_on_timeout=False timeout triggers best-effort cancel_execution and keeps timeout error.
        environment._client.create_execution.return_value = {
            "execution_id": "exec-queued-timeout",
            "state": "queued",
        }
        environment._client.execution_status.return_value = {
            "execution_id": "exec-queued-timeout",
            "state": "queued",
        }
        environment._client.cancel_execution.side_effect = client_mod.DsecControllerError(
            "state_conflict", "already running", retryable=False
        )
        with self.assertRaisesRegex(TimeoutError, "exec-queued-timeout"):
            await environment.exec("sleep 10", timeout_sec=0.002, close_on_timeout=False)
        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-queued-timeout"
        )
        environment._client.close_sandbox.assert_not_awaited()

    async def test_non_closing_cancellation_triggers_best_effort_cancel_without_closing_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._poll_interval_sec = 5.0
        environment._client.create_execution.return_value = {
            "execution_id": "exec-cancel-wait",
            "state": "queued",
        }
        environment._client.cancel_execution.return_value = {
            "execution_id": "exec-cancel-wait",
            "state": "canceled",
        }

        task = asyncio.create_task(
            environment._execute_argv(["sleep", "60"], close_on_timeout=False)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-cancel-wait"
        )
        environment._client.close_sandbox.assert_not_awaited()

    async def test_execution_timeout_cancels_single_command_without_closing_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-cancel-ok",
            "state": "running",
        }
        environment._client.execution_status.return_value = {
            "execution_id": "exec-cancel-ok",
            "state": "running",
        }
        environment._client.cancel_execution.return_value = {
            "execution_id": "exec-cancel-ok",
            "state": "canceled",
        }

        with self.assertRaisesRegex(TimeoutError, "exec-cancel-ok"):
            await environment._execute_argv(["sleep", "60"], timeout_sec=0.002)

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-cancel-ok"
        )
        environment._client.close_sandbox.assert_not_awaited()
        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_execution_timeout_falls_back_to_close_sandbox_when_cancel_conflicts(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-conflict",
            "state": "running",
        }
        environment._client.execution_status.return_value = {
            "execution_id": "exec-conflict",
            "state": "running",
        }
        environment._client.cancel_execution.side_effect = client_mod.DsecControllerError(
            "state_conflict", "backend does not support runtime cancel", retryable=False
        )
        environment._client.close_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "closed",
        }

        with self.assertRaisesRegex(TimeoutError, "exec-conflict"):
            await environment._execute_argv(["sleep", "60"], timeout_sec=0.002)

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-conflict"
        )
        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")
        self.assertEqual(environment._sandbox_id, "")

    async def test_execution_cancellation_cancels_single_command_without_closing_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._poll_interval_sec = 5.0
        environment._client.create_execution.return_value = {
            "execution_id": "exec-run-cancel",
            "state": "running",
        }
        environment._client.cancel_execution.return_value = {
            "execution_id": "exec-run-cancel",
            "state": "canceled",
        }

        task = asyncio.create_task(environment._execute_argv(["sleep", "60"]))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-run-cancel"
        )
        environment._client.close_sandbox.assert_not_awaited()
        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_execution_timeout_skips_close_when_conflict_shows_natural_terminal(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._poll_interval_sec = 0.005
        environment._client.create_execution.return_value = {
            "execution_id": "exec-finished-race",
            "state": "running",
        }
        environment._client.execution_status.side_effect = [
            {"execution_id": "exec-finished-race", "state": "running"},
            {"execution_id": "exec-finished-race", "state": "succeeded", "exit_code": 0},
        ]
        environment._client.cancel_execution.side_effect = client_mod.DsecControllerError(
            "state_conflict", "already succeeded", retryable=False
        )

        with self.assertRaisesRegex(TimeoutError, "exec-finished-race"):
            await environment._execute_argv(["sleep", "60"], timeout_sec=0.002)

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-finished-race"
        )
        self.assertEqual(environment._client.execution_status.await_count, 2)
        environment._client.close_sandbox.assert_not_awaited()
        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_double_cancellation_during_abort_still_completes_status_and_close_fallback(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        exec_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.execution"
        )
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._poll_interval_sec = 5.0
        environment._client.create_execution.return_value = {
            "execution_id": "exec-double-cancel",
            "state": "running",
        }
        cancel_started = asyncio.Event()
        cancel_release = asyncio.Event()
        close_done = asyncio.Event()

        async def blocking_cancel(_sid, _eid):
            cancel_started.set()
            await cancel_release.wait()
            raise client_mod.DsecControllerError(
                "state_conflict", "runtime cancel unavailable", retryable=False
            )

        async def close_and_mark(_sid):
            close_done.set()
            return {"sandbox_id": "sbx-one", "state": "closed"}

        environment._client.cancel_execution.side_effect = blocking_cancel
        environment._client.execution_status.return_value = {
            "execution_id": "exec-double-cancel",
            "state": "running",
        }
        environment._client.close_sandbox.side_effect = close_and_mark

        task = asyncio.create_task(environment._execute_argv(["sleep", "60"]))
        await asyncio.sleep(0.01)
        task.cancel()
        await cancel_started.wait()
        task.cancel()
        cancel_release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        await asyncio.wait_for(close_done.wait(), timeout=1.0)
        if exec_mod._BACKGROUND_ABORT_TASKS:
            await asyncio.gather(*list(exec_mod._BACKGROUND_ABORT_TASKS))

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-double-cancel"
        )
        environment._client.execution_status.assert_awaited_once_with(
            "sbx-one", "exec-double-cancel"
        )
        environment._client.close_sandbox.assert_awaited_once_with("sbx-one")
        self.assertEqual(environment._sandbox_id, "")

    def test_definition_accepts_harbor_outputs_and_read_only_inputs(self):
        environment = self.environment()
        environment.environment_dir = Path("/nonexistent-environment")
        environment._effective_storage_mb = 10240
        environment._mounts = [
            {
                "type": "bind",
                "source": "/host/agent",
                "target": "/logs/agent",
            },
            {
                "type": "bind",
                "source": "/host/cache",
                "target": "/opt/cache",
                "read_only": True,
            },
        ]

        with self.assertLogs("test-dsec", level="WARNING") as captured:
            adapter.DsecSandboxEnvironment._validate_definition(environment)

        self.assertIn("does not enforce per-Sandbox resource limits", captured.output[0])
        self.assertEqual(
            adapter.DsecSandboxEnvironment._resource_request(environment),
            {
                "policy": "shared/v1",
                "cpu_millicores": 0,
                "memory_bytes": 0,
                "pids_limit": 0,
            },
        )

        environment._mounts[0]["target"] = "/workspace"
        with self.assertRaisesRegex(ValueError, "Harbor-managed writable"):
            adapter.DsecSandboxEnvironment._validate_definition(environment)


class DsecTransferTests(unittest.IsolatedAsyncioTestCase):
    def environment(self, root):
        """Exercise real host tar operations with no executable guest runtime."""
        instance = object.__new__(adapter.DsecSandboxEnvironment)
        instance._transfer_max_bytes = 8 << 20
        instance._transfer_timeout_sec = 30
        instance._sandbox_id = "sandbox-one"
        instance.logger = logging.getLogger("test-dsec-transfer")
        instance.exec = AsyncMock(side_effect=AssertionError("guest shell forbidden"))
        instance._execute_argv = AsyncMock(side_effect=AssertionError("guest exec forbidden"))

        async def upload(_sid, destination, archive_path):
            destination = "/workspace" if destination == "." else destination
            target = root / destination.lstrip("/")
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "r:") as archive:
                archive.extractall(target, filter="fully_trusted")

        async def download(_sid, source, archive_path, maximum_bytes):
            path = root / source.lstrip("/")
            with tarfile.open(archive_path, "w") as archive:
                archive.add(path, arcname=path.name)
            if archive_path.stat().st_size > maximum_bytes:
                raise ValueError("DSec archive download exceeds configured limit")

        instance._client = SimpleNamespace(upload_archive=upload, download_archive=download)
        return instance

    async def test_file_roundtrip_preserves_binary_and_mode_without_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(root / "guest")
            source, downloaded = root / "source.bin", root / "downloaded.bin"
            payload = os.urandom((192 << 10) + 137)
            source.write_bytes(payload)
            source.chmod(0o4640)
            await environment.upload_file(source, "/remote/nested/payload.bin")
            await environment.download_file("/remote/nested/payload.bin", downloaded)
            self.assertEqual(downloaded.read_bytes(), payload)
            self.assertEqual(downloaded.stat().st_mode & 0o777, 0o640)

    async def test_directory_roundtrip_preserves_safe_symlinks_without_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(root / "guest")
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested/data.txt").write_text("payload")
            (source / "nested/data.txt").chmod(0o444)
            (source / "link.txt").symlink_to("nested/data.txt")
            await environment.upload_dir(source, "/remote")
            await environment.download_dir("/remote", root / "downloaded")
            self.assertEqual((root / "downloaded/nested/data.txt").read_text(), "payload")
            self.assertEqual((root / "downloaded/nested/data.txt").stat().st_mode & 0o777, 0o444)
            self.assertEqual(os.readlink(root / "downloaded/link.txt"), "nested/data.txt")

    async def test_directory_preparation_and_readonly_inputs_need_no_guest_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guest = root / "guest"
            environment = self.environment(guest)
            await environment._prepare_harbor_directories()
            self.assertEqual((guest / "logs/agent").stat().st_mode & 0o777, 0o777)
            source = root / "input.txt"
            source.write_text("readonly")
            environment._mounts = [
                {"source": str(source), "target": "/input.txt", "read_only": True},
                {"source": "/irrelevant-host-log", "target": "/logs/agent"},
            ]
            await environment._materialize_read_only_mounts()
            self.assertEqual((guest / "input.txt").read_text(), "readonly")
            self.assertEqual((guest / "input.txt").stat().st_mode & 0o222, 0)
            environment.exec.assert_not_awaited()
            environment._execute_argv.assert_not_awaited()

    async def test_relative_workdir_is_resolved_outside_guest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(root / "guest")
            source = root / "environment"
            source.mkdir()
            (source / "input.txt").write_text("workdir input")
            await environment.upload_dir(source, ".")
            self.assertEqual((root / "guest/workspace/input.txt").read_text(), "workdir input")

    async def test_failed_download_preserves_existing_local_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(root / "guest")
            target = root / "target.bin"
            target.write_bytes(b"keep")
            async def corrupt(_sid, _source, archive_path, _maximum):
                archive_path.write_bytes(b"not a tar archive")
            environment._client.download_archive = corrupt
            with self.assertRaises(tarfile.ReadError):
                await environment.download_file("/source.bin", target)
            self.assertEqual(target.read_bytes(), b"keep")

    def test_archive_path_escape_and_external_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as output:
                output.addfile(tarfile.TarInfo("../escape"))
            with self.assertRaisesRegex(ValueError, "unsafe member"):
                transfer._extract_directory(archive, root / "target")
            source = root / "source"
            source.mkdir()
            (source / "external").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "escapes its root"):
                transfer._pack_directory(source, root / "packed.tar", 1 << 20)

    async def test_is_dir_and_is_file_use_native_stat_without_exec(self):
        environment = self.environment(Path("/unused-guest"))
        environment.task_env_config = SimpleNamespace(workdir="/workspace")
        stats = {
            "/logs/agent": {"path": "/logs/agent", "exists": True, "type": "directory", "size": 4096},
            "/logs/agent/out.txt": {"path": "/logs/agent/out.txt", "exists": True, "type": "file", "size": 12},
            "/workspace/rel.txt": {"path": "/workspace/rel.txt", "exists": True, "type": "file", "size": 8},
            "/missing": {"path": "/missing", "exists": False},
            "/link-dir": {
                "path": "/link-dir", "exists": True, "type": "symlink",
                "link_target": "/logs/agent", "target_type": "directory",
            },
            "/link-file": {
                "path": "/link-file", "exists": True, "type": "symlink",
                "link_target": "/logs/agent/out.txt", "target_type": "file",
            },
            "/link-broken": {
                "path": "/link-broken", "exists": True, "type": "symlink",
                "link_target": "/logs/missing",
            },
            "/workspace": {"path": "/workspace", "exists": True, "type": "directory", "size": 4096},
        }
        seen_paths: list[str] = []

        async def fake_stat(sandbox_id: str, path: str):
            self.assertEqual(sandbox_id, "sandbox-one")
            seen_paths.append(path)
            return stats[path]

        environment._client.stat_path = fake_stat

        self.assertTrue(await environment.is_dir("/logs/agent/"))
        self.assertFalse(await environment.is_file("/logs/agent"))
        self.assertTrue(await environment.is_file("/logs/agent/out.txt"))
        self.assertFalse(await environment.is_dir("/logs/agent/out.txt"))
        self.assertTrue(await environment.is_file("rel.txt"))
        self.assertTrue(await environment.is_dir("."))
        self.assertFalse(await environment.is_dir("/missing"))
        self.assertFalse(await environment.is_file("/missing"))
        self.assertTrue(await environment.is_dir("/link-dir"))
        self.assertFalse(await environment.is_file("/link-dir"))
        self.assertTrue(await environment.is_file("/link-file"))
        self.assertFalse(await environment.is_dir("/link-file"))
        self.assertFalse(await environment.is_dir("/link-broken"))
        self.assertFalse(await environment.is_file("/link-broken"))
        self.assertIn("/workspace/rel.txt", seen_paths)
        self.assertIn("/workspace", seen_paths)
        self.assertNotIn(".", seen_paths)
        self.assertNotIn("/logs/missing", seen_paths)
        environment.exec.assert_not_awaited()
        environment._execute_argv.assert_not_awaited()

    async def test_stat_path_propagates_lifecycle_errors_and_parses_http_response(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            def __init__(self, status_code: int, body: bytes):
                self.status_code = status_code
                self.ok = 200 <= status_code < 300
                self._body = body
                self.closed = False

            def iter_content(self, _chunk_size):
                yield self._body

            def close(self):
                self.closed = True

        class FakeSession:
            trust_env = True

            def request(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["kwargs"] = kwargs
                return FakeResponse(
                    200,
                    b'{"api_version":"sandbox-lifecycle/v1alpha1","status":"ok","result":{"stat":{"path":"/missing","exists":false}}}',
                )

        client = client_mod.DsecControllerClient(
            "https://controller.example", session=FakeSession()
        )
        stat = await client.stat_path("sbx-1", "/missing")
        self.assertEqual(stat, {"path": "/missing", "exists": False})
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(
            captured["url"],
            "https://controller.example/v1alpha1/sandboxes/sbx-1/archive/stat?path=%2Fmissing",
        )

        environment = self.environment(Path("/unused-guest"))
        environment._client.stat_path = AsyncMock(
            side_effect=client_mod.DsecControllerError("state_conflict", "closing", retryable=False)
        )
        with self.assertRaises(client_mod.DsecControllerError):
            await environment.is_dir("/logs")

    async def test_probe_falls_back_to_exec_for_legacy_controller_and_preserves_real_missing_sandbox(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )

        class FakeResponse:
            def __init__(self, status_code: int, body: bytes):
                self.status_code = status_code
                self.ok = 200 <= status_code < 300
                self._body = body

            def iter_content(self, _chunk_size):
                yield self._body

            def close(self):
                pass

        class FakeSession:
            trust_env = True

            def request(self, _method, _url, **_kwargs):
                return FakeResponse(
                    404,
                    b'{"api_version":"sandbox-lifecycle/v1alpha1","status":"error","error":{"code":"not_found","message":"unknown subresource","retryable":false}}',
                )

        client = client_mod.DsecControllerClient(
            "https://controller.example", session=FakeSession()
        )
        with self.assertRaises(client_mod.DsecControllerError) as captured_exc:
            await client.stat_path("sbx-1", "/workspace/a.txt")
        self.assertEqual(captured_exc.exception.code, "not_found")
        self.assertEqual(captured_exc.exception.status_code, 404)

        environment = self.environment(Path("/unused-guest"))
        environment.task_env_config = SimpleNamespace(workdir="/workspace")
        environment._client.stat_path = AsyncMock(
            side_effect=client_mod.DsecControllerError(
                "not_found", "unknown subresource", retryable=False, status_code=404
            )
        )
        environment.exec = AsyncMock(return_value=SimpleNamespace(return_code=0))

        self.assertTrue(await environment.is_file("/workspace/a.txt"))
        self.assertFalse(environment._archive_stat_supported)
        self.assertTrue(await environment.is_dir("/workspace"))
        environment._client.stat_path.assert_awaited_once_with(
            "sandbox-one", "/workspace/a.txt"
        )
        self.assertEqual(
            environment.exec.await_args_list[0].args,
            ("test -f /workspace/a.txt",),
        )
        self.assertEqual(
            environment.exec.await_args_list[0].kwargs,
            {"user": None, "close_on_timeout": False},
        )
        self.assertEqual(
            environment.exec.await_args_list[1].args,
            ("test -d /workspace",),
        )
        self.assertEqual(
            environment.exec.await_args_list[1].kwargs,
            {"user": None, "close_on_timeout": False},
        )

        # unsupported_capability also falls back cleanly and caches support flag.
        unsupported_env = self.environment(Path("/unused-guest"))
        unsupported_env.task_env_config = SimpleNamespace(workdir="/workspace")
        unsupported_env._client.stat_path = AsyncMock(
            side_effect=client_mod.DsecControllerError(
                "unsupported_capability", "archive stat disabled", status_code=501
            )
        )
        unsupported_env.exec = AsyncMock(return_value=SimpleNamespace(return_code=1))
        self.assertFalse(await unsupported_env.is_dir("/workspace/missing"))
        self.assertFalse(unsupported_env._archive_stat_supported)

        # Real missing Sandbox propagates not_found from exec instead of returning False.
        missing_sandbox_env = self.environment(Path("/unused-guest"))
        missing_sandbox_env.task_env_config = SimpleNamespace(workdir="/workspace")
        missing_sandbox_env._client.stat_path = AsyncMock(
            side_effect=client_mod.DsecControllerError(
                "not_found", "sandbox not found", status_code=404
            )
        )
        missing_sandbox_env.exec = AsyncMock(
            side_effect=client_mod.DsecControllerError(
                "not_found", "sandbox not found", status_code=404
            )
        )
        with self.assertRaises(client_mod.DsecControllerError) as missing_exc:
            await missing_sandbox_env.is_file("/workspace/a.txt")
        self.assertEqual(missing_exc.exception.code, "not_found")
        self.assertTrue(getattr(missing_sandbox_env, "_archive_stat_supported", True))

        # Lifecycle errors must not fall back to exec.
        conflict_env = self.environment(Path("/unused-guest"))
        conflict_env.task_env_config = SimpleNamespace(workdir="/workspace")
        conflict_env._client.stat_path = AsyncMock(
            side_effect=client_mod.DsecControllerError(
                "recovery_required", "node down", status_code=409
            )
        )
        with self.assertRaises(client_mod.DsecControllerError):
            await conflict_env.is_file("/workspace/a.txt")
        conflict_env.exec.assert_not_awaited()

    async def test_probe_preserves_posix_trailing_slash_traversal_and_relative_symlink_semantics(self):
        environment = self.environment(Path("/unused-guest"))
        environment.task_env_config = SimpleNamespace(workdir="/workspace")
        environment._client.stat_path = AsyncMock(
            return_value={
                "path": "/workspace/rel-link",
                "exists": True,
                "type": "symlink",
                "link_target": "",
                "target_type": "",
            }
        )
        environment.exec = AsyncMock(return_value=SimpleNamespace(return_code=0))

        # Trailing slash/dot and "." / "/" cannot be regular files in POSIX.
        self.assertFalse(await environment.is_file("/workspace/a.txt/"))
        self.assertFalse(await environment.is_file("/workspace/a.txt/."))
        self.assertFalse(await environment.is_file("."))
        self.assertFalse(await environment.is_file("/"))
        environment._client.stat_path.assert_not_awaited()
        environment.exec.assert_not_awaited()

        # Traversal segments delegate to guest kernel resolution instead of lexical normpath.
        self.assertTrue(await environment.is_file("/workspace/link/../out.txt"))
        self.assertTrue(await environment.is_dir("/workspace/link/.."))
        self.assertTrue(await environment.is_dir("/workspace/dir/."))
        self.assertTrue(await environment.is_file("./rel.txt"))
        environment._client.stat_path.assert_not_awaited()
        self.assertEqual(
            [call.args[0] for call in environment.exec.await_args_list],
            [
                "test -f /workspace/link/../out.txt",
                "test -d /workspace/link/..",
                "test -d /workspace/dir/.",
                "test -f ./rel.txt",
            ],
        )
        self.assertTrue(
            all(
                call.kwargs == {"user": None, "close_on_timeout": False}
                for call in environment.exec.await_args_list
            )
        )
        self.assertTrue(getattr(environment, "_archive_stat_supported", True))

        # Relative symlink without server-side target_type falls back to kernel exec.
        environment.exec.reset_mock()
        environment.exec.return_value = SimpleNamespace(return_code=0)
        self.assertTrue(await environment.is_file("/workspace/rel-link"))
        environment._client.stat_path.assert_awaited_once_with(
            "sandbox-one", "/workspace/rel-link"
        )
        environment.exec.assert_awaited_once_with(
            "test -f /workspace/rel-link", user=None, close_on_timeout=False
        )

        # "." resolves against task_env_config.workdir rather than container create dir "/".
        environment.exec.reset_mock()
        environment._client.stat_path = AsyncMock(
            return_value={
                "path": "/workspace",
                "exists": True,
                "type": "directory",
                "size": 4096,
            }
        )
        self.assertTrue(await environment.is_dir("."))
        environment._client.stat_path.assert_awaited_once_with(
            "sandbox-one", "/workspace"
        )
        environment.exec.assert_not_awaited()

    async def test_client_cancel_execution_calls_expected_route(self):
        client_mod = importlib.import_module(
            "harbor_environment_adapters.providers.dsec_controller.client"
        )
        captured = {}

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self.ok = 200 <= status_code < 300
                self._body = body
                self.headers = {"content-type": "application/json"}
                self.closed = False

            def iter_content(self, _chunk_size):
                yield self._body

            def json(self):
                import json

                return json.loads(self._body.decode())

            def close(self):
                self.closed = True

        class FakeSession:
            trust_env = True

            def request(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["kwargs"] = kwargs
                return FakeResponse(
                    200,
                    b'{"api_version":"sandbox-lifecycle/v1alpha1","status":"ok","result":{"execution":{"execution_id":"exec-1","state":"canceled"}}}',
                )

        client = client_mod.DsecControllerClient(
            "https://controller.example", session=FakeSession()
        )
        res = await client.cancel_execution("sbx-1", "exec-1")
        self.assertEqual(res["state"], "canceled")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["url"],
            "https://controller.example/v1alpha1/sandboxes/sbx-1/executions/exec-1/cancel",
        )
        self.assertIsNone(captured["kwargs"].get("json"))


if __name__ == "__main__":
    unittest.main()
