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
            ["/bin/bash", "-c", "exit 7"],
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


if __name__ == "__main__":
    unittest.main()
