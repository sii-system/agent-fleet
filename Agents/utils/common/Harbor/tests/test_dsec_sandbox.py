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
from unittest.mock import AsyncMock

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
        environment._cleanup_remote_file = AsyncMock()
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
        environment._cleanup_remote_file.assert_awaited_once_with(argv[1])

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
    def environment(self):
        instance = object.__new__(adapter.DsecSandboxEnvironment)
        instance._transfer_max_bytes = 8 << 20
        instance._transfer_timeout_sec = 30
        instance.logger = logging.getLogger("test-dsec-transfer")
        instance.executed_argv = []

        async def execute_argv(argv, **_kwargs):
            instance.executed_argv.append(argv)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            state = "succeeded" if process.returncode == 0 else "failed"
            return (
                {"state": state, "exit_code": process.returncode},
                stdout,
                stderr,
            )

        async def execute_shell(command, cwd=None, **_kwargs):
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-lc",
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            return SimpleNamespace(
                return_code=process.returncode,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
            )

        instance._execute_argv = execute_argv
        instance.exec = execute_shell
        return instance

    async def test_file_roundtrip_is_chunked_checked_and_atomic(self):
        environment = self.environment()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            remote = root / "remote" / "payload.bin"
            downloaded = root / "downloaded.bin"
            payload = os.urandom(transfer.UPLOAD_CHUNK_BYTES + 137)
            source.write_bytes(payload)
            source.chmod(0o640)

            await environment.upload_file(source, str(remote))
            await environment.download_file(str(remote), downloaded)

            self.assertEqual(remote.read_bytes(), payload)
            self.assertEqual(downloaded.read_bytes(), payload)
            self.assertEqual(downloaded.stat().st_mode & 0o777, 0o640)
            self.assertTrue(
                all(
                    len(argument.encode()) <= 4096
                    for argv in environment.executed_argv
                    for argument in argv
                )
            )

    async def test_directory_roundtrip_preserves_safe_symlinks(self):
        environment = self.environment()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "nested").mkdir()
            (source / "nested" / "data.txt").write_text("payload")
            (source / "link.txt").symlink_to("nested/data.txt")
            remote = root / "remote"
            downloaded = root / "downloaded"

            await environment.upload_dir(source, str(remote))
            await environment.download_dir(str(remote), downloaded)

            self.assertEqual((downloaded / "nested" / "data.txt").read_text(), "payload")
            self.assertEqual(os.readlink(downloaded / "link.txt"), "nested/data.txt")

    async def test_materialization_skips_harbor_writable_outputs(self):
        environment = self.environment()
        environment.upload_dir = AsyncMock()
        environment.upload_file = AsyncMock()
        environment.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )
        environment._mounts = [
            {
                "type": "bind",
                "source": "/host/agent",
                "target": "/logs/agent",
            },
            {
                "type": "bind",
                "source": "/host/runtime.tar.gz",
                "target": "/opt/runtime.tar.gz",
                "read_only": True,
            },
        ]

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "runtime.tar.gz"
            source.write_bytes(b"runtime")
            environment._mounts[1]["source"] = str(source)
            await environment._materialize_read_only_mounts()

        environment.upload_file.assert_awaited_once()
        environment.upload_dir.assert_not_awaited()
        environment.exec.assert_awaited_once()

    def test_archive_path_escape_and_external_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo("../escape")
                member.size = 0
                output.addfile(member)
            with self.assertRaisesRegex(ValueError, "unsafe member"):
                transfer._extract_directory(archive, root / "target")

            source = root / "source"
            source.mkdir()
            (source / "external").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "absolute link"):
                transfer._pack_directory(source, root / "packed.tar.gz", 1 << 20)


if __name__ == "__main__":
    unittest.main()
