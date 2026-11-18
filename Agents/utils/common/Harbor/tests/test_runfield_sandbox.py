"""Test Runfield lifecycle, shared SDK pooling, UTF-8 chunking, and protocol boundaries."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    base = types.ModuleType("harbor.environments.base")
    capabilities = types.ModuleType("harbor.environments.capabilities")
    definition = types.ModuleType("harbor.environments.definition")

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
    definition.should_upload_environment_dir = lambda *_args, **_kwargs: False
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": environments,
            "harbor.environments.base": base,
            "harbor.environments.capabilities": capabilities,
            "harbor.environments.definition": definition,
        }
    )


install_harbor_stubs()
adapter = importlib.import_module(
    "harbor_environment_adapters.providers.runfield_controller.adapter"
)
client_mod = importlib.import_module(
    "harbor_environment_adapters.providers.runfield_controller.client"
)
exec_mod = importlib.import_module(
    "harbor_environment_adapters.providers.runfield_controller.execution"
)
transfer = importlib.import_module(
    "harbor_environment_adapters.providers.runfield_controller.transfer"
)


def make_stub_runfield_sdk():
    """Build an in-memory stub of sii-runfield for portable CI unit tests."""
    created_clients: list[FakeSandboxClient] = []

    @dataclasses.dataclass
    class ExecutionResult:
        execution: dict
        execution_id: str
        state: str
        exit_code: int | None
        stdout: bytes
        stderr: bytes

    class SandboxError(Exception):
        def __init__(
            self,
            code: str,
            *,
            request_id: str | None = None,
            sandbox: object | None = None,
            http_status: int | None = None,
            retryable: bool = False,
            outcome_unknown: bool = False,
        ) -> None:
            super().__init__(code)
            self.code = code
            self.request_id = request_id
            self.sandbox = sandbox
            self.http_status = http_status
            self.retryable = retryable
            self.outcome_unknown = outcome_unknown

    class FakeSandbox:
        def __init__(self, record: dict) -> None:
            self._record = dict(record)
            self.sandbox_id = record["sandbox_id"]
            self.request_id = record.get("request_id", "req-1")
            self.wait_ready_impl = None
            self.refresh_impl = None
            self.close_impl = None
            self.exec_impl = None
            self.wait_execution_impl = None
            self.get_execution_impl = None
            self.cancel_execution_impl = None
            self.stat_impl = None

        @property
        def state(self) -> str:
            return str(self._record["state"])

        @property
        def record(self) -> dict:
            return dict(self._record)

        async def wait_ready(self, *, timeout_seconds: float = 120):
            if self.wait_ready_impl is not None:
                await self.wait_ready_impl(self, timeout_seconds)
                return self
            self._record["state"] = "ready"
            self._record["observation"] = {"reachable": True, "node_id": "node-1"}
            return self

        async def refresh(self, *, timeout_seconds: float = 15):
            if self.refresh_impl is not None:
                await self.refresh_impl(self, timeout_seconds)
            return self

        async def close(self, *, wait: bool = False, timeout_seconds: float = 120):
            if self.close_impl is not None:
                await self.close_impl(self, wait, timeout_seconds)
                return self
            self._record["state"] = "closed" if wait else "closing"
            self._record["observation"] = {"reachable": True, "node_id": "node-1"}
            return self

        async def exec(self, argv, **kwargs) -> dict:
            if self.exec_impl is not None:
                return await self.exec_impl(argv, **kwargs)
            return {
                "sandbox_id": self.sandbox_id,
                "execution_id": "exec-1",
                "request_id": kwargs.get("request_id") or "req-exec-1",
                "state": "running",
            }

        async def wait_execution(self, execution_id: str, **kwargs) -> ExecutionResult:
            if self.wait_execution_impl is not None:
                return await self.wait_execution_impl(execution_id, **kwargs)
            return ExecutionResult(
                execution={
                    "sandbox_id": self.sandbox_id,
                    "execution_id": execution_id,
                    "state": "succeeded",
                    "exit_code": 0,
                    "stdout": {"committed_bytes": 2, "truncated": False},
                    "stderr": {"committed_bytes": 0, "truncated": False},
                },
                execution_id=execution_id,
                state="succeeded",
                exit_code=0,
                stdout=b"ok",
                stderr=b"",
            )

        async def get_execution(self, execution_id: str, *, timeout_seconds: float = 15) -> dict:
            if self.get_execution_impl is not None:
                return await self.get_execution_impl(execution_id, timeout_seconds)
            return {
                "sandbox_id": self.sandbox_id,
                "execution_id": execution_id,
                "state": "succeeded",
                "exit_code": 0,
            }

        async def cancel_execution(self, execution_id: str, *, timeout_seconds: float = 15) -> dict:
            if self.cancel_execution_impl is not None:
                return await self.cancel_execution_impl(execution_id, timeout_seconds)
            return {
                "sandbox_id": self.sandbox_id,
                "execution_id": execution_id,
                "state": "canceled",
            }

        async def stat(self, path: str, *, timeout_seconds: float = 15) -> dict:
            if self.stat_impl is not None:
                return await self.stat_impl(path, timeout_seconds)
            return {"path": path, "exists": False}

    class FakeSandboxClient:
        create_impl = None
        get_impl = None

        def __init__(
            self,
            *,
            endpoint: str,
            allow_insecure_http: bool = False,
            timeout_seconds: float = 15.0,
            proxy_socket: str | None = None,
            proxy_mode: str | None = None,
            proxy_binary: str | None = None,
            proxy_idle_timeout: str = "5m",
        ) -> None:
            self.endpoint = endpoint
            self.allow_insecure_http = allow_insecure_http
            self.timeout_seconds = timeout_seconds
            self.proxy_socket = proxy_socket
            self.proxy_mode = proxy_mode
            self.proxy_binary = proxy_binary
            self.proxy_idle_timeout = proxy_idle_timeout
            self.closed = False
            created_clients.append(self)

        async def create(
            self,
            spec: dict,
            *,
            request_id: str,
            wait_until: str = "ready",
            timeout_seconds: float = 120,
        ) -> FakeSandbox:
            if FakeSandboxClient.create_impl is not None:
                return await FakeSandboxClient.create_impl(
                    self, spec, request_id=request_id, wait_until=wait_until, timeout_seconds=timeout_seconds
                )
            return FakeSandbox(
                {
                    "sandbox_id": f"sbx-{len(created_clients)}-{request_id[-6:]}",
                    "request_id": request_id,
                    "state": "creating",
                    "observation": {"reachable": True, "node_id": "node-1"},
                }
            )

        async def get(self, sandbox_id: str, *, timeout_seconds: float = 15) -> FakeSandbox:
            if FakeSandboxClient.get_impl is not None:
                return await FakeSandboxClient.get_impl(self, sandbox_id, timeout_seconds=timeout_seconds)
            return FakeSandbox(
                {
                    "sandbox_id": sandbox_id,
                    "request_id": "req-fetched",
                    "state": "ready",
                    "observation": {"reachable": True, "node_id": "node-1"},
                }
            )

        async def aclose(self) -> None:
            self.closed = True

    mod = types.ModuleType("runfield")
    mod.ExecutionResult = ExecutionResult
    mod.Sandbox = FakeSandbox
    mod.SandboxClient = FakeSandboxClient
    mod.SandboxError = SandboxError
    return mod, created_clients


class RunfieldClientPoolingAndSdkTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        client_mod._LOOP_SHARED_STATES.clear()

    def test_lazy_sdk_loading_keeps_init_and_preflight_sdk_free(self) -> None:
        saved = sys.modules.pop("runfield", None)
        try:
            with patch.object(
                adapter.BaseEnvironment,
                "__init__",
                lambda self, **_kwargs: setattr(self, "_network_is_public", False),
            ), patch.dict(
                os.environ,
                {
                    "RUNFIELD_CONTROLLER_ENDPOINT": "https://controller.example",
                    "RUNFIELD_SANDBOX_TRANSFER_TIMEOUT_SEC": "37",
                },
            ):
                adapter.RunfieldSandboxEnvironment.preflight()
                environment = adapter.RunfieldSandboxEnvironment()
                self.assertEqual(environment._client.transfer_timeout_sec, 37)
                self.assertEqual(environment._client.request_timeout_sec, 15)
                self.assertNotIn("runfield", sys.modules)
        finally:
            if saved is not None:
                sys.modules["runfield"] = saved

    async def test_shared_sandbox_client_per_event_loop_and_proxy_forwarding(self) -> None:
        stub, created = make_stub_runfield_sdk()
        with patch.dict(sys.modules, {"runfield": stub}), patch.dict(
            os.environ,
            {"RUNFIELD_CLIENT_PROXY_SOCKET": "/tmp/runfield-test/proxy.sock"},
            clear=False,
        ):
            c1 = client_mod.RunfieldControllerClient("https://controller.example")
            c2 = client_mod.RunfieldControllerClient("https://controller.example")
            c_other = client_mod.RunfieldControllerClient("https://other.example")

            rec1 = await c1.create_sandbox(
                {
                    "request_id": "req-alpha-001",
                    "image": "registry.example/tasks/a@sha256:" + "a" * 64,
                    "resources": {"policy": "shared/v1"},
                    "lifecycle": {"ttl_seconds": 3600},
                }
            )
            rec2 = await c2.sandbox_status(rec1["sandbox_id"])
            self.assertEqual(rec2["sandbox_id"], rec1["sandbox_id"])
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].proxy_socket, "/tmp/runfield-test/proxy.sock")
            self.assertEqual(created[0].proxy_mode, "uds")
            self.assertIn(rec1["sandbox_id"], c1._sandboxes)
            self.assertIs(c1._sandboxes, c2._sandboxes)

            await c_other.sandbox_status("sbx-remote")
            self.assertEqual(len(created), 2)

            closed = await c2.close_sandbox(rec1["sandbox_id"], wait=True, timeout_sec=30)
            self.assertEqual(closed["state"], "closed")
            self.assertNotIn(rec1["sandbox_id"], c1._sandboxes)

            # Direct mode ignores ambient RUNFIELD_CLIENT_PROXY_SOCKET env var,
            # while RUNFIELD_CLIENT_PROXY_BIN separates shared pool entries.
            with patch.dict(
                os.environ,
                {"RUNFIELD_CLIENT_PROXY_MODE": "direct"},
                clear=False,
            ):
                c_direct = client_mod.RunfieldControllerClient("https://controller.example")
                await c_direct.sandbox_status("sbx-direct")
                self.assertEqual(len(created), 3)
                self.assertIsNone(created[-1].proxy_socket)
                self.assertEqual(created[-1].proxy_mode, "direct")

            c_explicit_conflict = client_mod.RunfieldControllerClient(
                "https://controller.example",
                proxy_socket="/tmp/runfield-test/explicit.sock",
                proxy_mode="direct",
            )
            self.assertEqual(
                c_explicit_conflict._resolve_proxy_settings(),
                ("/tmp/runfield-test/explicit.sock", "direct"),
            )

            with patch.dict(
                os.environ,
                {"RUNFIELD_CLIENT_PROXY_BIN": "/opt/custom/sandbox-client-proxy"},
                clear=False,
            ):
                c_bin = client_mod.RunfieldControllerClient("https://controller.example")
                await c_bin.sandbox_status("sbx-bin")
                self.assertEqual(len(created), 4)

    async def test_client_error_mappings_for_wait_ready_close_and_wait_execution(self) -> None:
        stub, _ = make_stub_runfield_sdk()
        with patch.dict(sys.modules, {"runfield": stub}):
            client = client_mod.RunfieldControllerClient("https://controller.example")
            rec = await client.create_sandbox(
                {
                    "request_id": "req-err-001",
                    "image": "registry.example/tasks/a@sha256:" + "a" * 64,
                    "resources": {"policy": "shared/v1"},
                    "lifecycle": {"ttl_seconds": 3600},
                }
            )
            sid = rec["sandbox_id"]
            sbx = client._sandboxes[sid]

            async def timeout_ready(_self, _timeout):
                raise stub.SandboxError("timeout", sandbox=_self)

            sbx.wait_ready_impl = timeout_ready
            with self.assertRaisesRegex(TimeoutError, f"wait deadline exceeded: {sid}"):
                await client.wait_sandbox_ready(sid, timeout_sec=5)

            async def conflict_ready(handle, _timeout):
                handle._record["state"] = "create_failed"
                raise stub.SandboxError("state_conflict", sandbox=handle)

            sbx.wait_ready_impl = conflict_ready
            with self.assertRaisesRegex(RuntimeError, "entered terminal state create_failed"):
                await client.wait_sandbox_ready(sid, timeout_sec=5)

            async def truncated_exec(_eid, **_kwargs):
                raise stub.SandboxError("resource_limit_exceeded", http_status=422)

            sbx.wait_execution_impl = truncated_exec
            with self.assertRaisesRegex(RuntimeError, "truncated or exceeded limit"):
                await client.wait_execution(
                    sid, "exec-trunc", timeout_sec=10, output_limit_bytes=1024
                )

            async def timeout_exec(_eid, **_kwargs):
                raise stub.SandboxError("timeout")

            sbx.wait_execution_impl = timeout_exec
            with self.assertRaisesRegex(
                TimeoutError, "execution wait deadline exceeded: exec-slow"
            ):
                await client.wait_execution(
                    sid, "exec-slow", timeout_sec=1, output_limit_bytes=1024
                )

            async def timeout_close(handle, _wait, _timeout):
                raise stub.SandboxError("timeout", sandbox=handle)

            sbx.close_impl = timeout_close
            with self.assertRaisesRegex(TimeoutError, f"close deadline exceeded: {sid}"):
                await client.close_sandbox(sid, wait=True, timeout_sec=2)
            self.assertIn(sid, client._sandboxes)

            # Purged/expired Sandboxes returning not_found on refresh or close are evicted.
            async def not_found_refresh(handle, _timeout):
                raise stub.SandboxError("not_found", sandbox=handle, http_status=404)

            sbx.refresh_impl = not_found_refresh
            with self.assertRaises(client_mod.RunfieldControllerError) as nf_status:
                await client.sandbox_status(sid)
            self.assertEqual(nf_status.exception.code, "not_found")
            self.assertNotIn(sid, client._sandboxes)

            rec2 = await client.create_sandbox(
                {
                    "request_id": "req-err-002",
                    "image": "registry.example/tasks/a@sha256:" + "a" * 64,
                    "resources": {"policy": "shared/v1"},
                    "lifecycle": {"ttl_seconds": 3600},
                }
            )
            sid2 = rec2["sandbox_id"]
            sbx2 = client._sandboxes[sid2]

            async def not_found_close(handle, _wait, _timeout):
                raise stub.SandboxError("not_found", sandbox=handle, http_status=404)

            sbx2.close_impl = not_found_close
            with self.assertRaises(client_mod.RunfieldControllerError) as nf_close:
                await client.close_sandbox(sid2, wait=True, timeout_sec=2)
            self.assertEqual(nf_close.exception.code, "not_found")
            self.assertNotIn(sid2, client._sandboxes)


class RunfieldLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def environment(self):
        instance = object.__new__(adapter.RunfieldSandboxEnvironment)
        instance._client = SimpleNamespace(
            create_sandbox=AsyncMock(),
            wait_sandbox_ready=AsyncMock(),
            sandbox_status=AsyncMock(),
            close_sandbox=AsyncMock(),
            create_execution=AsyncMock(),
            wait_execution=AsyncMock(),
            cancel_execution=AsyncMock(),
            execution_status=AsyncMock(),
            stat_path=AsyncMock(),
            upload_archive=AsyncMock(),
            download_archive=AsyncMock(),
        )
        instance._image_ref_override = (
            "registry.example/tasks/sample@sha256:" + "a" * 64
        )
        instance._bundle_manifest_path = ""
        instance._sandbox_id = ""
        instance._execution_sequence = 0
        instance._harbor_dirs_prepared = True
        instance._ttl_seconds = 3600
        instance._network_mode = "none"
        instance._ready_timeout_sec = 2
        instance._close_timeout_sec = 2
        instance._default_exec_timeout_sec = 2
        instance._exec_output_limit_bytes = 4096
        instance._transfer_max_bytes = 8 << 20
        instance._request_attempts = 3
        instance._poll_interval_sec = 0.001
        instance.session_id = "trial-one__env"
        instance.context_id = None
        instance.default_user = "root"
        instance.task_env_config = SimpleNamespace(
            docker_image=None, workdir="/workspace"
        )
        instance.logger = logging.getLogger("test-runfield")
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

    async def test_start_waits_ready_defers_harbor_dirs_and_uses_stable_spec(self):
        environment = self.environment()
        environment._client.create_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "creating",
        }
        environment._client.wait_sandbox_ready.return_value = {
            "sandbox_id": "sbx-one",
            "state": "ready",
            "observation": {"reachable": True},
        }

        await environment.start(force_build=False)

        self.assertEqual(environment._sandbox_id, "sbx-one")
        self.assertFalse(environment._harbor_dirs_prepared)
        environment._client.wait_sandbox_ready.assert_awaited_once_with(
            "sbx-one", timeout_sec=2
        )
        environment._client.upload_archive.assert_not_awaited()
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

    async def test_deferred_harbor_dirs_created_inline_on_first_exec_and_lazy_on_download(self):
        environment = self.environment()
        environment._client.create_sandbox.return_value = {
            "sandbox_id": "sbx-one",
            "state": "ready",
            "observation": {"reachable": True},
        }
        environment._client.create_execution.return_value = {
            "execution_id": "exec-1",
            "state": "succeeded",
            "exit_code": 0,
            "stdout": {"committed_bytes": 0, "truncated": False},
            "stderr": {"committed_bytes": 0, "truncated": False},
        }

        await environment.start(force_build=False)
        self.assertFalse(environment._harbor_dirs_prepared)

        # Before first exec, is_dir/is_file treat deferred Harbor output dirs as directories.
        environment._client.stat_path.return_value = {
            "path": "/logs/artifacts",
            "exists": False,
        }
        for deferred_dir in (
            "/logs",
            "/logs/agent",
            "/logs/verifier",
            "/logs/artifacts",
            "/artifacts",
        ):
            self.assertTrue(await environment.is_dir(deferred_dir))
            self.assertFalse(await environment.is_file(deferred_dir))
        self.assertFalse(await environment.is_dir("/unrelated/missing"))

        await environment.exec("echo first")
        first_argv = environment._client.create_execution.await_args.args[1]["argv"]
        self.assertEqual(
            first_argv,
            [
                "/bin/bash",
                "-c",
                'command -p mkdir -p -m 0777 /logs/agent /logs/verifier /logs/artifacts /artifacts || exit $?; exec /bin/bash -c "$1"',
                "bash",
                "echo first",
            ],
        )
        self.assertTrue(environment._harbor_dirs_prepared)
        self.assertFalse(await environment.is_dir("/logs/artifacts"))
        environment._client.wait_execution.assert_not_awaited()
        environment._client.upload_archive.assert_not_awaited()

        # Verify _HARBOR_DIRS_BOOTSTRAP enforces 0777 under umask 0077 + empty PATH,
        # preserves $? == 0 entering command, and aborts before command if mkdir fails.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_root = Path(tmp) / "root"
            sandbox_root.mkdir()
            rebased_bootstrap = exec_mod._HARBOR_DIRS_BOOTSTRAP.replace(
                " /", f" {sandbox_root}/"
            )
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                f"umask 0077; {rebased_bootstrap}printf '%s' \"$?\"",
                env={"PATH": "/nonexistent-bin"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_raw, err_raw = await proc.communicate()
            self.assertEqual(proc.returncode, 0, err_raw.decode(errors="replace"))
            self.assertEqual(out_raw, b"0")
            for rel in ("logs/agent", "logs/verifier", "logs/artifacts", "artifacts"):
                self.assertEqual((sandbox_root / rel).stat().st_mode & 0o777, 0o777)

            blocker = Path(tmp) / "blocker-file"
            blocker.write_text("not-a-dir", encoding="utf-8")
            failing_bootstrap = exec_mod._HARBOR_DIRS_BOOTSTRAP.replace(
                "/logs/agent", f"{blocker}/agent"
            ).replace(" /logs/", f" {sandbox_root}/logs/").replace(
                " /artifacts", f" {sandbox_root}/artifacts"
            )
            fail_proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                f"{failing_bootstrap}printf 'MUST_NOT_RUN'",
                env={"PATH": "/nonexistent-bin"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            fail_out, _ = await fail_proc.communicate()
            self.assertNotEqual(fail_proc.returncode, 0)
            self.assertEqual(fail_out, b"")

        await environment.exec("echo second")
        second_argv = environment._client.create_execution.await_args.args[1]["argv"]
        self.assertEqual(second_argv, ["/bin/bash", "-c", "echo second"])

        # Zero-exec trial lazily calls _prepare_harbor_directories before download_dir.
        zero_exec_env = self.environment()
        zero_exec_env._sandbox_id = "sbx-zero"
        zero_exec_env._harbor_dirs_prepared = False

        async def fake_download(_sid, _source, archive_path, _max_bytes):
            with tarfile.open(archive_path, "w") as tar:
                member = tarfile.TarInfo("agent")
                member.type, member.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(member)

        zero_exec_env._client.download_archive.side_effect = fake_download
        with tempfile.TemporaryDirectory() as tmp:
            await zero_exec_env.download_dir("/logs/agent", Path(tmp) / "out")
            self.assertTrue(zero_exec_env._harbor_dirs_prepared)
            zero_exec_env._client.upload_archive.assert_awaited_once()
            await zero_exec_env.download_dir("/logs/agent", Path(tmp) / "out2")
            zero_exec_env._client.upload_archive.assert_awaited_once()

    async def test_first_exec_with_deferred_harbor_cwd_materializes_directories_before_exec(
        self,
    ):
        for cwd_arg, workdir_cfg, expected_cwd in (
            ("/logs/agent", "/workspace", "/logs/agent"),
            ("/artifacts", "/workspace", "/artifacts"),
            ("/workspace/../logs/verifier", "/workspace", "/workspace/../logs/verifier"),
            ("sub", "/logs/agent", "sub"),
            (None, "/artifacts", "/artifacts"),
        ):
            with self.subTest(cwd=cwd_arg, workdir=workdir_cfg):
                environment = self.environment()
                environment.task_env_config = SimpleNamespace(
                    docker_image=None, workdir=workdir_cfg
                )
                environment._client.create_sandbox.return_value = {
                    "sandbox_id": "sbx-cwd",
                    "state": "ready",
                    "observation": {"reachable": True},
                }
                events: list[str] = []

                async def record_upload(
                    _sid, dest, archive_path, *, events_log=events
                ):
                    self.assertEqual(dest, "/")
                    self.assertTrue(Path(archive_path).is_file())
                    events_log.append("upload_archive")

                async def record_exec(
                    _sid,
                    _request,
                    *,
                    events_log=events,
                    env_obj=environment,
                ):
                    events_log.append("create_execution")
                    self.assertTrue(env_obj._harbor_dirs_prepared)
                    return {
                        "execution_id": "exec-cwd",
                        "state": "succeeded",
                        "exit_code": 0,
                        "stdout": {"committed_bytes": 0, "truncated": False},
                        "stderr": {"committed_bytes": 0, "truncated": False},
                    }

                environment._client.upload_archive.side_effect = record_upload
                environment._client.create_execution.side_effect = record_exec

                await environment.start(force_build=False)
                self.assertFalse(environment._harbor_dirs_prepared)

                await environment.exec("pwd", cwd=cwd_arg)

                self.assertEqual(events, ["upload_archive", "create_execution"])
                self.assertTrue(environment._harbor_dirs_prepared)
                req = environment._client.create_execution.await_args.args[1]
                self.assertEqual(req["argv"], ["/bin/bash", "-c", "pwd"])
                self.assertEqual(req["working_directory"], expected_cwd)

        # Ordinary working directories retain the 0-RPC inline directory bootstrap.
        for normal_cwd in ("/testbed", "/", "/workspace", "/logs2"):
            with self.subTest(normal_cwd=normal_cwd):
                environment = self.environment()
                environment._client.create_sandbox.return_value = {
                    "sandbox_id": "sbx-normal",
                    "state": "ready",
                    "observation": {"reachable": True},
                }
                environment._client.create_execution.return_value = {
                    "execution_id": "exec-normal",
                    "state": "succeeded",
                    "exit_code": 0,
                    "stdout": {"committed_bytes": 0, "truncated": False},
                    "stderr": {"committed_bytes": 0, "truncated": False},
                }
                await environment.start(force_build=False)
                await environment.exec("pwd", cwd=normal_cwd)
                environment._client.upload_archive.assert_not_awaited()
                req = environment._client.create_execution.await_args.args[1]
                self.assertEqual(
                    req["argv"],
                    [
                        "/bin/bash",
                        "-c",
                        exec_mod._DIRECT_HARBOR_DIRS_BOOTSTRAP,
                        "bash",
                        "pwd",
                    ],
                )

    async def test_unconfirmed_exec_failures_isolate_mkdir_and_keep_harbor_dirs_pending(
        self,
    ):
        async def run_rebased_bash(sandbox_root: Path, argv: list[str]):
            rebased = list(argv)
            rebased[2] = (
                rebased[2]
                .replace(" /logs/", f" {sandbox_root}/logs/")
                .replace(" /artifacts", f" {sandbox_root}/artifacts")
            )
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                'umask 0077; exec "$@"',
                "bash",
                *rebased,
                env={"PATH": "/nonexistent-bin"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            rc = proc.returncode if proc.returncode is not None else 1
            return (
                {
                    "execution_id": "exec-real-bash",
                    "state": "succeeded" if rc == 0 else "failed",
                    "exit_code": rc,
                    "stdout": {"committed_bytes": len(out_b), "truncated": False},
                    "stderr": {"committed_bytes": len(err_b), "truncated": False},
                },
                out_b,
                err_b,
            )

        # Case 1: exec("(") (both direct <=4096B and chunked >4096B) creates all 4
        # directories cleanly before inner /bin/bash fails with syntax error 2, AND
        # keeps _harbor_dirs_prepared == False so download_dir still ensures dirs.
        for bad_cmd in ("(", "(" + " " * 5000):
            with self.subTest(bad_cmd_len=len(bad_cmd)), tempfile.TemporaryDirectory() as tmp:
                sandbox_root = Path(tmp) / "guest"
                sandbox_root.mkdir()
                environment = self.environment()
                environment._client.create_sandbox.return_value = {
                    "sandbox_id": "sbx-syntax",
                    "state": "ready",
                    "observation": {"reachable": True},
                }

                async def execute_against_host_bash(
                    argv, root=sandbox_root, **_kwargs
                ):
                    return await run_rebased_bash(root, argv)

                environment._execute_argv = AsyncMock(
                    side_effect=execute_against_host_bash
                )
                await environment.start(force_build=False)
                self.assertFalse(environment._harbor_dirs_prepared)

                bad_res = await environment.exec(bad_cmd)
                self.assertEqual(bad_res.return_code, 2)
                self.assertIn("syntax error", bad_res.stderr)
                for rel in (
                    "logs/agent",
                    "logs/verifier",
                    "logs/artifacts",
                    "artifacts",
                ):
                    created_dir = sandbox_root / rel
                    self.assertTrue(created_dir.is_dir())
                    self.assertEqual(created_dir.stat().st_mode & 0o777, 0o777)
                self.assertFalse(environment._harbor_dirs_prepared)

                async def fake_download(_sid, _source, archive_path, _max_bytes):
                    with tarfile.open(archive_path, "w") as tar:
                        member = tarfile.TarInfo("agent")
                        member.type, member.mode = tarfile.DIRTYPE, 0o777
                        tar.addfile(member)

                environment._client.download_archive.side_effect = fake_download
                await environment.download_dir("/logs/agent", Path(tmp) / "out")
                environment._client.upload_archive.assert_awaited_once()
                self.assertTrue(environment._harbor_dirs_prepared)

        # Case 2: failing mkdir (exit 1) aborts before user command, keeps
        # _harbor_dirs_prepared == False, and retries bootstrap on second exec("true").
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_root = Path(tmp) / "guest-fail-mkdir"
            sandbox_root.mkdir()
            blocker = sandbox_root / "logs"
            blocker.write_text("file-blocking-mkdir", encoding="utf-8")

            environment = self.environment()
            environment._client.create_sandbox.return_value = {
                "sandbox_id": "sbx-mkdir-fail",
                "state": "ready",
                "observation": {"reachable": True},
            }

            async def execute_mkdir_case(argv, **_kwargs):
                return await run_rebased_bash(sandbox_root, argv)

            environment._execute_argv = AsyncMock(side_effect=execute_mkdir_case)
            await environment.start(force_build=False)
            self.assertFalse(environment._harbor_dirs_prepared)

            first = await environment.exec("printf 'MUST_NOT_RUN'")
            self.assertNotEqual(first.return_code, 0)
            self.assertEqual(first.stdout, "")
            self.assertFalse(environment._harbor_dirs_prepared)

            blocker.unlink()
            second = await environment.exec("true")
            self.assertEqual(second.return_code, 0)
            self.assertTrue(environment._harbor_dirs_prepared)
            second_argv = environment._execute_argv.await_args.args[0]
            self.assertEqual(
                second_argv,
                [
                    "/bin/bash",
                    "-c",
                    exec_mod._DIRECT_HARBOR_DIRS_BOOTSTRAP,
                    "bash",
                    "true",
                ],
            )
            for rel in (
                "logs/agent",
                "logs/verifier",
                "logs/artifacts",
                "artifacts",
            ):
                self.assertEqual(
                    (sandbox_root / rel).stat().st_mode & 0o777, 0o777
                )

            third = await environment.exec("true")
            self.assertEqual(third.return_code, 0)
            self.assertEqual(
                environment._execute_argv.await_args.args[0],
                ["/bin/bash", "-c", "true"],
            )

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

        with self.assertLogs("test-runfield", level="WARNING") as captured:
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

        environment._client.close_sandbox.assert_awaited_once_with(
            "sbx-one", wait=True, timeout_sec=2
        )
        self.assertEqual(environment._sandbox_id, "")

    async def test_stop_retains_sandbox_identity_when_close_request_fails(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.close_sandbox.side_effect = RuntimeError(
            "controller unavailable"
        )

        with self.assertRaisesRegex(RuntimeError, "controller unavailable"):
            await environment.stop(delete=True)

        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_stop_retains_sandbox_identity_when_close_wait_times_out(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.close_sandbox.side_effect = TimeoutError("close timed out")

        with self.assertRaisesRegex(TimeoutError, "close timed out"):
            await environment.stop(delete=True)

        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_exec_preserves_nonzero_exit_and_rejects_user_switch(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-one",
            "state": "running",
        }
        environment._client.wait_execution.return_value = (
            {
                "execution_id": "exec-one",
                "state": "failed",
                "exit_code": 7,
                "stdout": {"committed_bytes": 2, "truncated": False},
                "stderr": {"committed_bytes": 0, "truncated": False},
            },
            b"ok",
            b"",
        )

        result = await environment.exec("exit 7", user="root")

        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(
            environment._client.create_execution.await_args.args[1]["argv"],
            ["/bin/bash", "-c", "exit 7"],
        )
        with self.assertRaisesRegex(ValueError, "root execution identity"):
            await environment.exec("true", user="1000")

    async def test_inline_utf8_chunked_command_preserves_multibyte_boundaries_without_archive(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment.upload_file = AsyncMock(
            side_effect=AssertionError("4KB..96KB commands must not call upload_file")
        )
        environment._execute_argv = AsyncMock(
            return_value=(
                {
                    "state": "succeeded",
                    "exit_code": 0,
                    "stdout": {"committed_bytes": 0, "truncated": False},
                    "stderr": {"committed_bytes": 0, "truncated": False},
                },
                b"chunked-ok",
                b"",
            )
        )
        # Construct a ~16 KiB command where 3-byte CJK and 4-byte emoji sit
        # directly across 4096-byte UTF-8 boundaries.
        payload_text = ("a" * 4094 + "中文🚀" + "b" * 4093 + "🧪测试") * 2
        command = f"printf '%s' {json.dumps(payload_text, ensure_ascii=False)}"
        raw_bytes = command.encode("utf-8")
        self.assertGreater(len(raw_bytes), 4096)
        self.assertLessEqual(len(raw_bytes), 98304)

        result = await environment.exec(command)

        self.assertEqual(result.stdout, "chunked-ok")
        environment.upload_file.assert_not_awaited()
        argv = environment._execute_argv.await_args.args[0]
        self.assertEqual(argv[:2], ["/bin/bash", "-c"])
        self.assertEqual(argv[3], "bash")
        self.assertRegex(
            argv[2],
            r'^__rf_cmd_[0-9a-f]{16}=""; '
            r'while \[ "\$#" -gt 0 \]; do __rf_cmd_[0-9a-f]{16}\+="\$1"; shift; done; '
            r'export -n __rf_cmd_[0-9a-f]{16}; exec /bin/bash -c "\$__rf_cmd_[0-9a-f]{16}"$',
        )
        self.assertLessEqual(len(argv), 256)
        for item in argv:
            self.assertLessEqual(len(item.encode("utf-8")), 4096)
        self.assertEqual("".join(argv[4:]), command)

        # Verify real /bin/bash reconstructs and runs the chunked argv byte-for-byte.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await proc.communicate()
        self.assertEqual(proc.returncode, 0, stderr_raw.decode(errors="replace"))
        self.assertEqual(stdout_raw.decode(), payload_text)

    async def test_chunked_command_preserves_caller_s_and_p_env_trailing_newlines_and_exit_code(
        self,
    ):
        probe_body = (
            'printf "s=%s(%s)|p=%s(%s)|rf=%s|leaked=%s|argc=%s\\n" '
            '"${s-UNSET}" "${s+SET}" "${p-UNSET}" "${p+SET}" '
            '"${__rf_cmd-UNSET}" "$(compgen -v __rf_cmd_ | tr \'\\n\' \',\')" "$#"\n'
            "cat <<'__HARBOR_EOF__'\n"
            "line-with-trailing-newlines\n"
            "\n"
            "__HARBOR_EOF__\n"
            "exit 23\n\n\n"
        )
        direct_cmd = probe_body
        chunked_cmd = ("# " + ("x" * 5200) + "\n") + probe_body
        self.assertLessEqual(len(direct_cmd.encode("utf-8")), 4096)
        self.assertGreater(len(chunked_cmd.encode("utf-8")), 5000)

        with tempfile.TemporaryDirectory() as tmp:
            allexport_env_file = Path(tmp) / "bash_env_allexport.sh"
            allexport_env_file.write_text("set -a\n", encoding="utf-8")

            for env_case in (
                {
                    "PATH": os.defpath,
                    "s": "caller-s",
                    "p": "caller-p",
                    "__rf_cmd": "caller-rf",
                },
                {
                    "PATH": os.defpath,
                    "s": "allexport-s",
                    "p": "allexport-p",
                    "BASH_ENV": str(allexport_env_file),
                },
                {"PATH": os.defpath},
            ):
                with self.subTest(env_keys=sorted(env_case.keys())):
                    outputs: list[tuple[int, bytes, bytes]] = []
                    for cmd in (direct_cmd, chunked_cmd):
                        environment = self.environment()
                        environment._sandbox_id = "sbx-env-parity"
                        environment._persistent_env = dict(env_case)

                        async def run_real_bash(argv, *, env=None, **_kwargs):
                            proc = await asyncio.create_subprocess_exec(
                                *argv,
                                env=env,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            out_b, err_b = await proc.communicate()
                            rc = proc.returncode if proc.returncode is not None else 1
                            return (
                                {
                                    "execution_id": "exec-bash",
                                    "state": "succeeded" if rc == 0 else "failed",
                                    "exit_code": rc,
                                    "stdout": {
                                        "committed_bytes": len(out_b),
                                        "truncated": False,
                                    },
                                    "stderr": {
                                        "committed_bytes": len(err_b),
                                        "truncated": False,
                                    },
                                },
                                out_b,
                                err_b,
                            )

                        environment._execute_argv = AsyncMock(
                            side_effect=run_real_bash
                        )
                        res = await environment.exec(cmd, env=dict(env_case))
                        outputs.append(
                            (
                                res.return_code,
                                res.stdout.encode("utf-8"),
                                res.stderr.encode("utf-8"),
                            )
                        )

                    self.assertEqual(outputs[0][0], 23)
                    self.assertEqual(outputs[0], outputs[1])
                    if "s" in env_case:
                        expected_prefix = (
                            f"s={env_case['s']}(SET)|p={env_case['p']}(SET)|"
                        ).encode()
                        self.assertTrue(outputs[1][1].startswith(expected_prefix))
                    else:
                        self.assertTrue(
                            outputs[1][1].startswith(b"s=UNSET()|p=UNSET()|")
                        )
                    self.assertIn(b"|leaked=|argc=0\n", outputs[1][1])

    async def test_oversized_or_nul_command_falls_back_to_materialized_upload(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        uploaded: list[bytes] = []

        async def capture_upload(source, _target):
            uploaded.append(Path(source).read_bytes())

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
        huge_command = "printf x # " + "x" * 98304
        self.assertGreater(len(huge_command.encode("utf-8")), 98304)

        result = await environment.exec(huge_command)

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(uploaded, [huge_command.encode("utf-8")])
        argv = environment._execute_argv.await_args.args[0]
        self.assertEqual(argv[0], "/bin/bash")
        self.assertEqual(len(argv), 2)
        self.assertTrue(argv[1].startswith("/tmp/.harbor-command-"))

        await environment.exec("printf 'a\x00b'")
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(uploaded[1], b"printf 'a\x00b'")

    async def test_materialized_first_exec_isolates_harbor_dir_bootstrap_and_tracks_completion(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_root = Path(tmp) / "guest"
            (sandbox_root / "tmp").mkdir(parents=True)
            environment = self.environment()
            environment._client.create_sandbox.return_value = {
                "sandbox_id": "sbx-mat-first",
                "state": "ready",
                "observation": {"reachable": True},
            }
            uploaded_scripts: list[tuple[str, bytes]] = []

            async def stage_script(source, target):
                payload = Path(source).read_bytes()
                uploaded_scripts.append((target, payload))
                host_script = sandbox_root / target.lstrip("/")
                host_script.parent.mkdir(parents=True, exist_ok=True)
                host_script.write_bytes(payload)
                host_script.chmod(0o600)

            async def run_materialized_on_host_bash(argv, **_kwargs):
                rebased = list(argv)
                if len(rebased) == 5:
                    rebased[2] = (
                        rebased[2]
                        .replace(" /logs/", f" {sandbox_root}/logs/")
                        .replace(" /artifacts", f" {sandbox_root}/artifacts")
                    )
                    rebased[4] = str(sandbox_root / rebased[4].lstrip("/"))
                else:
                    rebased[1] = str(sandbox_root / rebased[1].lstrip("/"))
                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash",
                    "-c",
                    'umask 0077; exec "$@"',
                    "bash",
                    *rebased,
                    env={"PATH": os.defpath},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out_b, err_b = await proc.communicate()
                rc = proc.returncode if proc.returncode is not None else 1
                return (
                    {
                        "execution_id": "exec-mat",
                        "state": "succeeded" if rc == 0 else "failed",
                        "exit_code": rc,
                        "stdout": {
                            "committed_bytes": len(out_b),
                            "truncated": False,
                        },
                        "stderr": {
                            "committed_bytes": len(err_b),
                            "truncated": False,
                        },
                    },
                    out_b,
                    err_b,
                )

            environment.upload_file = AsyncMock(side_effect=stage_script)
            environment._execute_argv = AsyncMock(
                side_effect=run_materialized_on_host_bash
            )
            await environment.start(force_build=False)
            self.assertFalse(environment._harbor_dirs_prepared)

            bad_huge = "(" + " " * 98304
            bad_res = await environment.exec(bad_huge)
            self.assertEqual(bad_res.return_code, 2)
            self.assertIn("syntax error", bad_res.stderr)
            self.assertEqual(uploaded_scripts[0][1], bad_huge.encode())
            first_argv = environment._execute_argv.await_args.args[0]
            self.assertEqual(len(first_argv), 5)
            self.assertEqual(
                first_argv[:4],
                [
                    "/bin/bash",
                    "-c",
                    exec_mod._MATERIALIZED_HARBOR_DIRS_BOOTSTRAP,
                    "bash",
                ],
            )
            self.assertTrue(first_argv[4].startswith("/tmp/.harbor-command-"))
            for rel in (
                "logs/agent",
                "logs/verifier",
                "logs/artifacts",
                "artifacts",
            ):
                self.assertEqual(
                    (sandbox_root / rel).stat().st_mode & 0o777, 0o777
                )
            self.assertFalse(environment._harbor_dirs_prepared)

            good_huge = "printf materialized-ok # " + "x" * 98304
            good_res = await environment.exec(good_huge)
            self.assertEqual(good_res.return_code, 0)
            self.assertEqual(good_res.stdout, "materialized-ok")
            self.assertEqual(uploaded_scripts[1][1], good_huge.encode())
            second_argv = environment._execute_argv.await_args.args[0]
            self.assertEqual(
                second_argv[:4],
                [
                    "/bin/bash",
                    "-c",
                    exec_mod._MATERIALIZED_HARBOR_DIRS_BOOTSTRAP,
                    "bash",
                ],
            )
            self.assertTrue(environment._harbor_dirs_prepared)

            after_res = await environment.exec("printf after-ok # " + "z" * 98304)
            self.assertEqual(after_res.return_code, 0)
            self.assertEqual(after_res.stdout, "after-ok")
            third_argv = environment._execute_argv.await_args.args[0]
            self.assertEqual(len(third_argv), 2)
            self.assertEqual(third_argv[0], "/bin/bash")
            self.assertTrue(third_argv[1].startswith("/tmp/.harbor-command-"))

            # Also verify NUL-containing first exec uses 5-arg bootstrap, creates
            # Harbor dirs cleanly even when /bin/bash rejects binary script (126),
            # and keeps _harbor_dirs_prepared == False.
            nul_root = Path(tmp) / "guest-nul"
            (nul_root / "tmp").mkdir(parents=True)
            sandbox_root = nul_root
            environment._harbor_dirs_prepared = False
            nul_res = await environment.exec("printf 'done\x00ok'")
            self.assertEqual(nul_res.return_code, 126)
            nul_argv = environment._execute_argv.await_args.args[0]
            self.assertEqual(len(nul_argv), 5)
            self.assertEqual(
                nul_argv[:4],
                [
                    "/bin/bash",
                    "-c",
                    exec_mod._MATERIALIZED_HARBOR_DIRS_BOOTSTRAP,
                    "bash",
                ],
            )
            for rel in (
                "logs/agent",
                "logs/verifier",
                "logs/artifacts",
                "artifacts",
            ):
                self.assertEqual(
                    (nul_root / rel).stat().st_mode & 0o777, 0o777
                )
            self.assertFalse(environment._harbor_dirs_prepared)

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
        environment._client.close_sandbox.assert_awaited_once_with(
            "sbx-one", wait=True, timeout_sec=2
        )

    async def test_execution_cancellation_before_execution_id_closes_sandbox(self):
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

        environment._client.close_sandbox.assert_awaited_once_with(
            "sbx-one", wait=True, timeout_sec=2
        )
        self.assertEqual(environment._sandbox_id, "")

    async def test_exec_forwards_priority_and_cancels_queued_orphan_on_non_closing_timeout(self):
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

        environment._client.create_execution.return_value = {
            "execution_id": "exec-queued-timeout",
            "state": "queued",
        }
        environment._client.wait_execution.side_effect = TimeoutError(
            "Runfield execution wait deadline exceeded: exec-queued-timeout"
        )
        environment._client.cancel_execution.side_effect = (
            client_mod.RunfieldControllerError(
                "state_conflict", "already running", retryable=False
            )
        )
        with self.assertRaisesRegex(TimeoutError, "exec-queued-timeout"):
            await environment.exec(
                "sleep 10", timeout_sec=0.002, close_on_timeout=False
            )
        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-queued-timeout"
        )
        environment._client.close_sandbox.assert_not_awaited()

    async def test_non_closing_cancellation_triggers_best_effort_cancel_without_closing_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        wait_started = asyncio.Event()

        async def block_wait(*_args, **_kwargs):
            wait_started.set()
            await asyncio.Event().wait()

        environment._client.create_execution.return_value = {
            "execution_id": "exec-cancel-wait",
            "state": "queued",
        }
        environment._client.wait_execution.side_effect = block_wait
        environment._client.cancel_execution.return_value = {
            "execution_id": "exec-cancel-wait",
            "state": "canceled",
        }

        task = asyncio.create_task(
            environment._execute_argv(["sleep", "60"], close_on_timeout=False)
        )
        await wait_started.wait()
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
        environment._client.wait_execution.side_effect = TimeoutError(
            "Runfield execution wait deadline exceeded: exec-cancel-ok"
        )
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
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-conflict",
            "state": "running",
        }
        environment._client.wait_execution.side_effect = TimeoutError(
            "Runfield execution wait deadline exceeded: exec-conflict"
        )
        environment._client.execution_status.return_value = {
            "execution_id": "exec-conflict",
            "state": "running",
        }
        environment._client.cancel_execution.side_effect = (
            client_mod.RunfieldControllerError(
                "state_conflict",
                "backend does not support runtime cancel",
                retryable=False,
            )
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
        environment._client.execution_status.assert_awaited_once_with(
            "sbx-one", "exec-conflict"
        )
        environment._client.close_sandbox.assert_awaited_once_with(
            "sbx-one", wait=True, timeout_sec=2
        )
        self.assertEqual(environment._sandbox_id, "")

    async def test_execution_cancellation_cancels_single_command_without_closing_sandbox(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        wait_started = asyncio.Event()

        async def block_wait(*_args, **_kwargs):
            wait_started.set()
            await asyncio.Event().wait()

        environment._client.create_execution.return_value = {
            "execution_id": "exec-run-cancel",
            "state": "running",
        }
        environment._client.wait_execution.side_effect = block_wait
        environment._client.cancel_execution.return_value = {
            "execution_id": "exec-run-cancel",
            "state": "canceled",
        }

        task = asyncio.create_task(environment._execute_argv(["sleep", "60"]))
        await wait_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-run-cancel"
        )
        environment._client.close_sandbox.assert_not_awaited()
        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_execution_timeout_skips_close_when_conflict_shows_natural_terminal(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        environment._client.create_execution.return_value = {
            "execution_id": "exec-finished-race",
            "state": "running",
        }
        environment._client.wait_execution.side_effect = TimeoutError(
            "Runfield execution wait deadline exceeded: exec-finished-race"
        )
        environment._client.execution_status.return_value = {
            "execution_id": "exec-finished-race",
            "state": "succeeded",
            "exit_code": 0,
        }
        environment._client.cancel_execution.side_effect = (
            client_mod.RunfieldControllerError(
                "state_conflict", "already succeeded", retryable=False
            )
        )

        with self.assertRaisesRegex(TimeoutError, "exec-finished-race"):
            await environment._execute_argv(["sleep", "60"], timeout_sec=0.002)

        environment._client.cancel_execution.assert_awaited_once_with(
            "sbx-one", "exec-finished-race"
        )
        environment._client.execution_status.assert_awaited_once_with(
            "sbx-one", "exec-finished-race"
        )
        environment._client.close_sandbox.assert_not_awaited()
        self.assertEqual(environment._sandbox_id, "sbx-one")

    async def test_double_cancellation_during_abort_still_completes_status_and_close_fallback(self):
        environment = self.environment()
        environment._sandbox_id = "sbx-one"
        wait_started = asyncio.Event()
        cancel_started = asyncio.Event()
        cancel_release = asyncio.Event()
        close_done = asyncio.Event()

        async def block_wait(*_args, **_kwargs):
            wait_started.set()
            await asyncio.Event().wait()

        async def blocking_cancel(_sid, _eid):
            cancel_started.set()
            await cancel_release.wait()
            raise client_mod.RunfieldControllerError(
                "state_conflict", "runtime cancel unavailable", retryable=False
            )

        async def close_and_mark(_sid, **_kwargs):
            close_done.set()
            return {"sandbox_id": "sbx-one", "state": "closed"}

        environment._client.create_execution.return_value = {
            "execution_id": "exec-double-cancel",
            "state": "running",
        }
        environment._client.wait_execution.side_effect = block_wait
        environment._client.cancel_execution.side_effect = blocking_cancel
        environment._client.execution_status.return_value = {
            "execution_id": "exec-double-cancel",
            "state": "running",
        }
        environment._client.close_sandbox.side_effect = close_and_mark

        task = asyncio.create_task(environment._execute_argv(["sleep", "60"]))
        await wait_started.wait()
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
        environment._client.close_sandbox.assert_awaited_once_with(
            "sbx-one", wait=True, timeout_sec=2
        )
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

        with self.assertLogs("test-runfield", level="WARNING") as captured:
            adapter.RunfieldSandboxEnvironment._validate_definition(environment)

        self.assertIn("does not enforce per-Sandbox resource limits", captured.output[0])
        self.assertEqual(
            adapter.RunfieldSandboxEnvironment._resource_request(environment),
            {
                "policy": "shared/v1",
                "cpu_millicores": 0,
                "memory_bytes": 0,
                "pids_limit": 0,
            },
        )

        environment._mounts[0]["target"] = "/workspace"
        with self.assertRaisesRegex(ValueError, "Harbor-managed writable"):
            adapter.RunfieldSandboxEnvironment._validate_definition(environment)


class RunfieldTransferTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        client_mod._LOOP_SHARED_STATES.clear()

    def environment(self, root):
        """Exercise real host tar operations with no executable guest runtime."""
        instance = object.__new__(adapter.RunfieldSandboxEnvironment)
        instance._transfer_max_bytes = 8 << 20
        instance._transfer_timeout_sec = 30
        instance._sandbox_id = "sandbox-one"
        instance._harbor_dirs_prepared = True
        instance.logger = logging.getLogger("test-runfield-transfer")
        instance.exec = AsyncMock(side_effect=AssertionError("guest shell forbidden"))
        instance._execute_argv = AsyncMock(
            side_effect=AssertionError("guest exec forbidden")
        )

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
                raise ValueError("Runfield archive download exceeds configured limit")

        instance._client = SimpleNamespace(
            upload_archive=upload, download_archive=download
        )
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
            self.assertEqual(
                (root / "downloaded/nested/data.txt").read_text(), "payload"
            )
            self.assertEqual(
                (root / "downloaded/nested/data.txt").stat().st_mode & 0o777, 0o444
            )
            self.assertEqual(os.readlink(root / "downloaded/link.txt"), "nested/data.txt")

    async def test_directory_preparation_and_readonly_inputs_need_no_guest_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guest = root / "guest"
            environment = self.environment(guest)
            environment._harbor_dirs_prepared = False
            await environment._prepare_harbor_directories()
            self.assertTrue(environment._harbor_dirs_prepared)
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
            self.assertEqual(
                (root / "guest/workspace/input.txt").read_text(), "workdir input"
            )

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
            "/logs/agent": {
                "path": "/logs/agent",
                "exists": True,
                "type": "directory",
                "size": 4096,
            },
            "/logs/agent/out.txt": {
                "path": "/logs/agent/out.txt",
                "exists": True,
                "type": "file",
                "size": 12,
            },
            "/workspace/rel.txt": {
                "path": "/workspace/rel.txt",
                "exists": True,
                "type": "file",
                "size": 8,
            },
            "/missing": {"path": "/missing", "exists": False},
            "/link-dir": {
                "path": "/link-dir",
                "exists": True,
                "type": "symlink",
                "link_target": "/logs/agent",
                "target_type": "directory",
            },
            "/link-file": {
                "path": "/link-file",
                "exists": True,
                "type": "symlink",
                "link_target": "/logs/agent/out.txt",
                "target_type": "file",
            },
            "/link-broken": {
                "path": "/link-broken",
                "exists": True,
                "type": "symlink",
                "link_target": "/logs/missing",
            },
            "/workspace": {
                "path": "/workspace",
                "exists": True,
                "type": "directory",
                "size": 4096,
            },
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

    async def test_stat_path_and_cancel_execution_translate_sdk_responses(self):
        stub, _ = make_stub_runfield_sdk()
        captured: dict[str, object] = {}

        async def custom_get(_client, sandbox_id: str, *, timeout_seconds: float = 15):
            sbx = stub.Sandbox(
                {
                    "sandbox_id": sandbox_id,
                    "request_id": "req-1",
                    "state": "ready",
                    "observation": {"reachable": True, "node_id": "node-1"},
                }
            )

            async def stat_impl(path: str, timeout: float):
                captured["stat"] = (sandbox_id, path, timeout)
                return {"path": path, "exists": False}

            async def cancel_impl(execution_id: str, timeout: float):
                captured["cancel"] = (sandbox_id, execution_id, timeout)
                return {
                    "sandbox_id": sandbox_id,
                    "execution_id": execution_id,
                    "state": "canceled",
                }

            sbx.stat_impl = stat_impl
            sbx.cancel_execution_impl = cancel_impl
            return sbx

        stub.SandboxClient.get_impl = custom_get
        with patch.dict(sys.modules, {"runfield": stub}):
            client = client_mod.RunfieldControllerClient(
                "https://controller.example", request_timeout_sec=12
            )
            stat = await client.stat_path("sbx-1", "/missing")
            self.assertEqual(stat, {"path": "/missing", "exists": False})
            self.assertEqual(captured["stat"], ("sbx-1", "/missing", 12))

            canceled = await client.cancel_execution("sbx-1", "exec-1")
            self.assertEqual(canceled["state"], "canceled")
            self.assertEqual(captured["cancel"], ("sbx-1", "exec-1", 12))

        environment = self.environment(Path("/unused-guest"))
        environment._client.stat_path = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
                "state_conflict", "closing", retryable=False
            )
        )
        with self.assertRaises(client_mod.RunfieldControllerError):
            await environment.is_dir("/logs")

    async def test_probe_falls_back_to_exec_for_legacy_controller_and_preserves_real_missing_sandbox(self):
        stub, _ = make_stub_runfield_sdk()

        async def failing_get(_client, sandbox_id: str, *, timeout_seconds: float = 15):
            _ = timeout_seconds
            sbx = stub.Sandbox(
                {
                    "sandbox_id": sandbox_id,
                    "request_id": "req-1",
                    "state": "ready",
                    "observation": {"reachable": True, "node_id": "node-1"},
                }
            )

            async def not_found_stat(_path: str, _timeout: float):
                raise stub.SandboxError(
                    "not_found", http_status=404, retryable=False
                )

            sbx.stat_impl = not_found_stat
            return sbx

        stub.SandboxClient.get_impl = failing_get
        with patch.dict(sys.modules, {"runfield": stub}):
            client = client_mod.RunfieldControllerClient("https://controller.example")
            with self.assertRaises(client_mod.RunfieldControllerError) as captured_exc:
                await client.stat_path("sbx-1", "/workspace/a.txt")
            self.assertEqual(captured_exc.exception.code, "not_found")
            self.assertEqual(captured_exc.exception.status_code, 404)

        environment = self.environment(Path("/unused-guest"))
        environment.task_env_config = SimpleNamespace(workdir="/workspace")
        environment._client.stat_path = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
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

        unsupported_env = self.environment(Path("/unused-guest"))
        unsupported_env.task_env_config = SimpleNamespace(workdir="/workspace")
        unsupported_env._client.stat_path = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
                "unsupported_capability", "archive stat disabled", status_code=501
            )
        )
        unsupported_env.exec = AsyncMock(return_value=SimpleNamespace(return_code=1))
        self.assertFalse(await unsupported_env.is_dir("/workspace/missing"))
        self.assertFalse(unsupported_env._archive_stat_supported)

        missing_sandbox_env = self.environment(Path("/unused-guest"))
        missing_sandbox_env.task_env_config = SimpleNamespace(workdir="/workspace")
        missing_sandbox_env._client.stat_path = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
                "not_found", "sandbox not found", status_code=404
            )
        )
        missing_sandbox_env.exec = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
                "not_found", "sandbox not found", status_code=404
            )
        )
        with self.assertRaises(client_mod.RunfieldControllerError) as missing_exc:
            await missing_sandbox_env.is_file("/workspace/a.txt")
        self.assertEqual(missing_exc.exception.code, "not_found")
        self.assertTrue(getattr(missing_sandbox_env, "_archive_stat_supported", True))

        conflict_env = self.environment(Path("/unused-guest"))
        conflict_env.task_env_config = SimpleNamespace(workdir="/workspace")
        conflict_env._client.stat_path = AsyncMock(
            side_effect=client_mod.RunfieldControllerError(
                "recovery_required", "node down", status_code=409
            )
        )
        with self.assertRaises(client_mod.RunfieldControllerError):
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

        self.assertFalse(await environment.is_file("/workspace/a.txt/"))
        self.assertFalse(await environment.is_file("/workspace/a.txt/."))
        self.assertFalse(await environment.is_file("."))
        self.assertFalse(await environment.is_file("/"))
        environment._client.stat_path.assert_not_awaited()
        environment.exec.assert_not_awaited()

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

        environment.exec.reset_mock()
        environment.exec.return_value = SimpleNamespace(return_code=0)
        self.assertTrue(await environment.is_file("/workspace/rel-link"))
        environment._client.stat_path.assert_awaited_once_with(
            "sandbox-one", "/workspace/rel-link"
        )
        environment.exec.assert_awaited_once_with(
            "test -f /workspace/rel-link", user=None, close_on_timeout=False
        )

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


@unittest.skipUnless(
    importlib.util.find_spec("runfield") is not None,
    "sii-runfield SDK not installed in portable CI",
)
class RunfieldRealSdkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise RunfieldSandboxEnvironment end-to-end through the real sii-runfield SDK."""

    def tearDown(self) -> None:
        client_mod._LOOP_SHARED_STATES.clear()

    async def test_end_to_end_adapter_over_real_sii_runfield_sdk(self) -> None:
        sandboxes: dict[str, dict] = {}
        executions: dict[str, dict] = {}
        uploaded_archives: list[bytes] = []
        release_oversized_tail = threading.Event()
        oversized_tail_reached = False
        runfield_mod = importlib.import_module("runfield")
        sdk_supports_max_bytes = (
            "max_bytes"
            in inspect.signature(runfield_mod.Sandbox.download_archive).parameters
        )
        spec_digest = "sha256:" + "1" * 64

        class V1Alpha1Handler(BaseHTTPRequestHandler):
            def _json_reply(self, status: int, result: dict) -> None:
                body = json.dumps(
                    {
                        "api_version": "sandbox-lifecycle/v1alpha1",
                        "status": "ok",
                        "result": result,
                    }
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else {}
                if parsed.path == "/v1alpha1/sandboxes":
                    sid = "sbx-live-001"
                    rec = {
                        "sandbox_id": sid,
                        "request_id": payload["request_id"],
                        "image": payload["image"],
                        "spec_digest": spec_digest,
                        "actual_delivery_mode": "lazy-required",
                        "state": "creating",
                        "revision": 1,
                        "observation": {"reachable": True, "node_id": "node-live-1"},
                    }
                    sandboxes[sid] = rec
                    self._json_reply(202, {"sandbox": rec})
                    return
                if parsed.path.endswith("/close"):
                    sid = parsed.path.split("/")[3]
                    rec = dict(sandboxes[sid])
                    rec["state"] = "closed"
                    rec["revision"] += 1
                    sandboxes[sid] = rec
                    self._json_reply(200, {"sandbox": rec})
                    return
                if parsed.path.endswith("/executions"):
                    sid = parsed.path.split("/")[3]
                    eid = f"exec-{len(executions) + 1}"
                    argv = payload["argv"]
                    cmd_text = "".join(argv[4:]) if len(argv) > 4 else argv[-1]
                    is_slow = "sleep 30" in cmd_text
                    out = f"ran:{len(cmd_text)}".encode()
                    exec_rec = {
                        "sandbox_id": sid,
                        "execution_id": eid,
                        "request_id": payload["request_id"],
                        "spec_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "state": "running" if is_slow else "succeeded",
                        "revision": 1,
                        "priority": payload.get("priority", ""),
                        "exit_code": None if is_slow else 0,
                        "stdout": {
                            "committed_bytes": 0 if is_slow else len(out),
                            "truncated": False,
                            "availability": "available",
                        },
                        "stderr": {
                            "committed_bytes": 0,
                            "truncated": False,
                            "availability": "available",
                        },
                        "_stdout_bytes": out,
                    }
                    executions[eid] = exec_rec
                    public_rec = {
                        k: v for k, v in exec_rec.items() if not k.startswith("_")
                    }
                    self._json_reply(202, {"execution": public_rec})
                    return
                if parsed.path.endswith("/cancel"):
                    eid = parsed.path.split("/")[5]
                    exec_rec = executions[eid]
                    exec_rec["state"] = "canceled"
                    exec_rec["revision"] += 1
                    public_rec = {
                        k: v for k, v in exec_rec.items() if not k.startswith("_")
                    }
                    self._json_reply(200, {"execution": public_rec})
                    return
                self.send_error(404)

            def do_PUT(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                uploaded_archives.append(self.rfile.read(length))
                self._json_reply(200, {})

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 3 and parts[:2] == ["v1alpha1", "sandboxes"]:
                    sid = parts[2]
                    rec = dict(sandboxes[sid])
                    if rec["state"] == "creating":
                        rec["state"] = "ready"
                        rec["revision"] += 1
                        sandboxes[sid] = rec
                    self._json_reply(200, {"sandbox": rec})
                    return
                if len(parts) == 5 and parts[3] == "archive" and parts[4] == "stat":
                    query = parse_qs(parsed.query)
                    path = query["path"][0]
                    exists = path == "/workspace"
                    stat_payload: dict[str, object] = {"path": path, "exists": exists}
                    if exists:
                        stat_payload.update({"type": "directory", "size": 4096})
                    self._json_reply(200, {"stat": stat_payload})
                    return
                if len(parts) == 4 and parts[3] == "archive":
                    query = parse_qs(parsed.query)
                    req_path = (query.get("path") or [""])[0]
                    if req_path == "/workspace/oversized.bin":
                        nonlocal oversized_tail_reached
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-tar")
                        self.end_headers()
                        self.wfile.write(b"x" * 4096)
                        self.wfile.flush()
                        if sdk_supports_max_bytes:
                            release_oversized_tail.wait(timeout=5.0)
                        else:
                            oversized_tail_reached = True
                        self.close_connection = True
                        return
                    buf = io.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w") as tar:
                        info = tarfile.TarInfo("out.txt")
                        data = b"downloaded-content"
                        info.size, info.mode = len(data), 0o644
                        tar.addfile(info, io.BytesIO(data))
                    raw = buf.getvalue()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-tar")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if len(parts) == 5 and parts[3] == "executions":
                    eid = parts[4]
                    exec_rec = executions[eid]
                    public_rec = {
                        k: v for k, v in exec_rec.items() if not k.startswith("_")
                    }
                    self._json_reply(200, {"execution": public_rec})
                    return
                if len(parts) == 7 and parts[3] == "executions" and parts[5] == "output":
                    eid, stream = parts[4], parts[6]
                    data = (
                        executions[eid]["_stdout_bytes"] if stream == "stdout" else b""
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), V1Alpha1Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as tmpdir, patch.object(
                adapter.BaseEnvironment,
                "__init__",
                lambda self, **_kwargs: (
                    setattr(self, "_network_is_public", False),
                    setattr(self, "environment_dir", Path(tmpdir) / "env"),
                    setattr(self, "session_id", "live-trial__env"),
                    setattr(self, "context_id", None),
                    setattr(
                        self,
                        "task_env_config",
                        SimpleNamespace(docker_image=None, workdir="/workspace"),
                    ),
                    setattr(self, "logger", logging.getLogger("test-live-sdk")),
                    setattr(self, "_persistent_env", {"K": "V"}),
                    setattr(self, "_effective_cpus", None),
                    setattr(self, "_effective_memory_mb", None),
                    setattr(self, "_effective_storage_mb", None),
                    setattr(self, "_mounts", []),
                )[-1],
            ), patch.dict(
                os.environ,
                {
                    "RUNFIELD_CONTROLLER_ENDPOINT": endpoint,
                    "RUNFIELD_CONTROLLER_ALLOW_INSECURE_HTTP": "1",
                    "RUNFIELD_CLIENT_PROXY_SOCKET": "/tmp/ambient-socket-ignored-in-direct.sock",
                    "RUNFIELD_CLIENT_PROXY_MODE": "direct",
                },
                clear=False,
            ):
                env = adapter.RunfieldSandboxEnvironment(
                    image_ref="registry.example/tasks/demo@sha256:" + "b" * 64,
                )
                try:
                    await env.start(force_build=False)
                    self.assertEqual(env._sandbox_id, "sbx-live-001")
                    self.assertEqual(uploaded_archives, [])
                    self.assertTrue(await env.is_dir("/workspace"))
                    self.assertTrue(await env.is_dir("/logs/artifacts"))
                    self.assertFalse(await env.is_file("/logs/artifacts"))

                    chunked_cmd = "echo " + ("中🚀" * 1200)
                    res = await env.exec(chunked_cmd)
                    self.assertEqual(res.return_code, 0)
                    self.assertTrue(res.stdout.startswith("ran:"))
                    self.assertEqual(uploaded_archives, [])

                    src_file = Path(tmpdir) / "in.txt"
                    src_file.write_text("hello", encoding="utf-8")
                    await env.upload_file(src_file, "/workspace/in.txt")
                    self.assertEqual(len(uploaded_archives), 1)

                    dst_file = Path(tmpdir) / "out.txt"
                    await env.download_file("/workspace/out.txt", dst_file)
                    self.assertEqual(dst_file.read_bytes(), b"downloaded-content")

                    live_sbx = env._client._sandboxes["sbx-live-001"]
                    orig_download = live_sbx.download_archive
                    recorded_download_kwargs: list[dict[str, object]] = []

                    @functools.wraps(orig_download)
                    async def spy_download(path: str, *args: object, **kwargs: object):
                        recorded_download_kwargs.append(dict(kwargs))
                        return await orig_download(path, *args, **kwargs)

                    live_sbx.download_archive = spy_download
                    oversized_dst = Path(tmpdir) / "oversized.tar"
                    try:
                        with self.assertRaisesRegex(
                            ValueError, "exceeds configured limit"
                        ):
                            await asyncio.wait_for(
                                env._client.download_archive(
                                    "sbx-live-001",
                                    "/workspace/oversized.bin",
                                    oversized_dst,
                                    1024,
                                ),
                                timeout=2.0,
                            )
                        self.assertFalse(oversized_dst.exists())
                        if sdk_supports_max_bytes:
                            self.assertEqual(
                                recorded_download_kwargs[-1].get("max_bytes"), 1024
                            )
                            self.assertFalse(oversized_tail_reached)
                            self.assertFalse(release_oversized_tail.is_set())
                    finally:
                        release_oversized_tail.set()

                    with self.assertRaises(TimeoutError):
                        await env.exec("sleep 30", timeout_sec=0.35)
                    self.assertEqual(env._sandbox_id, "sbx-live-001")

                    await env.stop(delete=True)
                    self.assertEqual(env._sandbox_id, "")
                    self.assertEqual(env._client._sandboxes, {})
                finally:
                    await env._client.aclose()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
