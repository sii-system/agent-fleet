"""Offline tests against Harbor's real Windows environment/agent interfaces."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from kubevirt_windows.agent import WindowsCommandAgent
from kubevirt_windows.control import (
    OWNER_LABEL,
    Cluster,
    Settings,
)
from kubevirt_windows.environment import KubeVirtWindowsEnvironment
from kubevirt_windows.transport import WAATransport, ps_quote, windows_path


def make_settings() -> Settings:
    return Settings(
        cluster=Cluster(
            api_server="https://10.254.64.34:6443",
            ca_data="CA",
            client_cert_data="CERT",
            client_key_data="KEY",
        ),
        image="windows-golden",
        namespace="default",
        node="cpu-nat-391",
    )


class PathTests(unittest.TestCase):
    def test_windows_paths_and_quoting(self):
        self.assertEqual(windows_path(r"C:\任务\a b.txt"), "C:/任务/a b.txt")
        self.assertEqual(ps_quote("a'b"), "'a''b'")

    def test_reject_ambiguous_windows_paths(self):
        for path in [
            "/tmp/file",
            "C:relative",
            "C:/../escape",
            "C:/x:stream",
            "C:/x\nget other",
            "//server/share",
            "C:/a*",
            "C:/a\x00",
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                windows_path(path)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.settings = make_settings()
        self.transport = WAATransport(
            self.settings, "trial", "192.0.2.100", root
        )

    async def asyncTearDown(self):
        await self.transport.close()

    async def test_request_uses_file_not_command_interpolation(self):
        captured = {}

        async def upload(source, target):
            captured.update(json.loads(Path(source).read_text()))

        self.transport.upload_file = AsyncMock(side_effect=upload)
        self.transport.powershell = AsyncMock(
            side_effect=["", '{"stdout":"ok","stderr":"","return_code":7,"timed_out":false}']
        )
        result = await self.transport.execute(
            "echo %KEY% & exit /b 7",
            cwd="C:/任务",
            env={"KEY": "fake-secret"},
            timeout=9,
        )
        self.assertEqual(captured["env"], {"KEY": "fake-secret"})
        self.assertEqual(result["return_code"], 7)
        self.assertNotIn("fake-secret", self.transport.powershell.call_args.args[0])
        self.assertNotIn("echo", self.transport.powershell.call_args.args[0])

    async def test_remote_timeout_raises(self):
        self.transport.upload_file = AsyncMock()
        self.transport.powershell = AsyncMock(side_effect=["", '{"timed_out":true}'])
        with self.assertRaises(TimeoutError):
            await self.transport.execute("sleep", cwd="C:/", env={}, timeout=1)

    async def test_cancellation_signals_remote_supervisor(self):
        self.transport.upload_file = AsyncMock()
        self.transport.powershell = AsyncMock(
            side_effect=[asyncio.CancelledError(), ""]
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.transport.execute("sleep", cwd="C:/", env={}, timeout=1)
        self.assertEqual(self.transport.powershell.await_count, 2)
        self.assertIn(".cancel", self.transport.powershell.call_args.args[0])

    async def test_reject_guest_traversal(self):
        for paths in [
            ["../../escape"],
            ["/absolute"],
            ["C:/absolute"],
            ["a\\b"],
            {"bad": "type"},
        ]:
            self.transport.powershell = AsyncMock(return_value=json.dumps(paths))
            with self.subTest(paths=paths), self.assertRaises((TypeError, ValueError)):
                await self.transport.list_files("C:/logs")

    async def test_empty_and_unicode_file_lists(self):
        for paths in [[], ["任务/a b.txt", "normal.txt"]]:
            self.transport.powershell = AsyncMock(return_value=json.dumps(paths))
            self.assertEqual(await self.transport.list_files("C:/logs"), paths)


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = make_settings()
        patcher = patch.object(Settings, "from_env", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def environment(self, **config):
        environment = KubeVirtWindowsEnvironment(
            environment_dir=self.root / "environment",
            environment_name="windows-test",
            session_id="test-trial",
            trial_paths=TrialPaths(self.root / "trial"),
            task_env_config=EnvironmentConfig(os="windows", **config),
        )
        self.addCleanup(lambda: environment._log_snapshot.cleanup() if environment._log_snapshot else None)
        return environment

    async def test_real_harbor_contract_and_env_precedence(self):
        environment = self.environment(env={"SOURCE": "persistent"})
        self.assertTrue(environment.capabilities.windows)
        self.assertFalse(environment.capabilities.mounted)
        environment._started = True
        environment.transport = AsyncMock()
        environment.transport.execute.return_value = {
            "stdout": "hello",
            "stderr": "err",
            "return_code": 3,
        }
        callback = AsyncMock()
        with (
            environment.scoped_exec_env({"SOURCE": "scoped"}),
            environment.scoped_output_callback(callback),
        ):
            result = await environment.exec("exit /b 3", env={"SOURCE": "per-exec"})
        self.assertEqual(result.return_code, 3)
        self.assertEqual(
            environment.transport.execute.call_args.kwargs["env"]["SOURCE"], "scoped"
        )
        self.assertEqual(callback.await_count, 2)
        await environment.exec("echo next")
        self.assertEqual(
            environment.transport.execute.call_args.kwargs["env"]["SOURCE"],
            "persistent",
        )

    async def test_reject_user_impersonation_before_command(self):
        environment = self.environment()
        with self.assertRaisesRegex(ValueError, "guest server user"):
            await environment.exec("echo x", user="root")

    def test_unsupported_resource_and_network_requirements_fail(self):
        for config in [
            {"storage_mb": 123},
            {"network_mode": "no-network"},
            {"gpus": 1},
            {"docker_image": "example/windows"},
        ]:
            with (
                self.subTest(config=config),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.environment(**config)

    async def test_start_waits_for_guest_and_creates_log_dirs(self):
        environment = self.environment(cpus=8, memory_mb=16384)
        environment.control.create = AsyncMock()
        environment.control.start = AsyncMock()
        environment.control.get = AsyncMock(
            return_value={
                "name": environment.vm_name,
                "namespace": "default",
                "ip": "10.254.73.30",
                "ready": True,
                "status": "Running",
                "uid": "x",
                "labels": {OWNER_LABEL: environment.token[:12]},
            }
        )
        environment.control.expose_guest = AsyncMock(return_value="10.254.73.30:30050")
        with patch("kubevirt_windows.environment.WAATransport") as transport_class:
            transport_class.return_value = AsyncMock()
            await environment.start()
        self.assertTrue(environment._started)
        environment.control.create.assert_awaited_once()
        _, kwargs = environment.control.create.await_args
        self.assertEqual(kwargs.get("cpus"), 8)
        self.assertEqual(kwargs.get("memory_mb"), 16384)
        environment.control.start.assert_awaited_once_with(environment.vm_name)
        environment.control.expose_guest.assert_awaited_once_with(
            environment.vm_name, owner=environment.token[:12]
        )
        transport_class.assert_called_once_with(
            environment.settings,
            environment.vm_name,
            "10.254.73.30",
            environment._local_dir.name,
            guest_port=30050,
        )
        environment.transport.probe.assert_awaited_once()
        self.assertEqual(environment.transport.mkdir.await_count, 4)
        metadata = json.loads((self.root / "trial/kubevirt.json").read_text())
        self.assertEqual(metadata["vm"], environment.vm_name)
        self.assertEqual(metadata["source_pvc"], environment.settings.image)
        self.assertNotIn("fake-key", json.dumps(metadata))
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        await environment.stop()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(
            environment.vm_name, owner=environment.token[:12]
        )

    async def test_failed_start_cleans_up(self):
        environment = self.environment()
        environment.control.create = AsyncMock(
            side_effect=RuntimeError("creation failed")
        )
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        environment.control.get = AsyncMock(
            return_value={"labels": {OWNER_LABEL: environment.token[:12]}}
        )
        with self.assertRaisesRegex(RuntimeError, "creation failed"):
            await environment.start()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(
            environment.vm_name, owner=environment.token[:12]
        )
        environment.control.close.assert_awaited_once()
        self.assertIsNone(environment._local_dir)

    async def test_ale_start_selects_transport_and_retries_guest_readiness(self):
        settings = replace(self.settings, guest_protocol="ale", waa_port=8000)
        with patch.object(Settings, "from_env", return_value=settings):
            environment = self.environment()
        environment.control.create = AsyncMock()
        environment.control.start = AsyncMock()
        environment.control.get = AsyncMock(return_value={"ready": True, "ip": "192.0.2.10"})
        environment.control.expose_guest = AsyncMock(return_value="192.0.2.10:30080")
        environment.control.close = AsyncMock()
        guest = AsyncMock()
        guest.probe.side_effect = [RuntimeError("booting"), None]
        with (
            patch("kubevirt_windows.environment.ALETransport", return_value=guest) as ale_class,
            patch("kubevirt_windows.environment.WAATransport") as waa_class,
            patch("kubevirt_windows.environment.asyncio.sleep", new_callable=AsyncMock),
        ):
            await environment.start()
        waa_class.assert_not_called()
        ale_class.assert_called_once_with(
            settings, environment.vm_name, "192.0.2.10", environment._local_dir.name,
            guest_port=30080,
        )
        self.assertEqual(guest.probe.await_count, 2)
        guest.prepare.assert_awaited_once()
        self.assertEqual(guest.mkdir.await_count, 4)
        metadata = json.loads((self.root / "trial/kubevirt.json").read_text())
        self.assertEqual(metadata["guest_protocol"], "ale")
        environment.control.get.return_value = {"labels": {OWNER_LABEL: environment.token[:12]}}
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        await environment.stop()
        guest.close.assert_awaited_once()
        environment.control.delete.assert_awaited_once()

    async def test_cancelled_start_cleans_up(self):
        environment = self.environment()
        environment.control.create = AsyncMock(side_effect=asyncio.CancelledError)
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        environment.control.get = AsyncMock(
            return_value={"labels": {OWNER_LABEL: environment.token[:12]}}
        )
        with self.assertRaises(asyncio.CancelledError):
            await environment.start()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(
            environment.vm_name, owner=environment.token[:12]
        )
        environment.control.close.assert_awaited_once()

    async def test_stop_refuses_vm_owned_by_another_trial(self):
        environment = self.environment()
        environment._created = environment._started = True
        environment.transport = AsyncMock()
        environment.control = AsyncMock()
        environment.control.get.return_value = {"labels": {OWNER_LABEL: "another-trial"}}
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            await environment.stop()
        environment.control.stop.assert_not_awaited()
        environment.control.delete.assert_not_awaited()
        environment.transport.list_files.assert_not_awaited()

    async def test_retained_vm_cannot_be_started_again(self):
        environment = self.environment()
        environment._created = True
        environment.control = AsyncMock()
        environment.control.get.return_value = {
            "labels": {OWNER_LABEL: environment.token[:12]}
        }
        await environment.stop(delete=False)
        environment.control.delete.assert_not_awaited()
        self.assertTrue(environment._created)
        with self.assertRaisesRegex(RuntimeError, "retained"):
            await environment.start()

    async def test_stop_before_start_never_mutates_platform(self):
        environment = self.environment()
        environment.control = AsyncMock()
        await environment.stop()
        environment.control.stop.assert_not_awaited()
        environment.control.delete.assert_not_awaited()

    async def test_create_failure_releases_local_resources(self):
        environment = self.environment()
        environment.control = AsyncMock()
        environment.control.create.side_effect = RuntimeError("create failed")
        # Once create is attempted the VM may be partially defined; cleanup must
        # still track ownership and release local resources after the delete.
        environment.control.get.return_value = {
            "labels": {OWNER_LABEL: environment.token[:12]}
        }
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "create failed"):
            await environment.start()
        environment.control.stop.assert_awaited_once()
        environment.control.delete.assert_awaited_once()
        environment.control.close.assert_awaited_once()
        self.assertIsNone(environment._local_dir)

    async def test_already_deleted_vm_is_idempotent(self):
        import httpx

        environment = self.environment()
        environment._created = True
        environment.control = AsyncMock()
        environment.control.get.side_effect = httpx.HTTPStatusError(
            "missing", request=httpx.Request("GET", "http://x"), response=httpx.Response(404)
        )
        await environment.stop()
        await environment.stop()
        environment.control.get.assert_awaited_once()
        environment.control.stop.assert_not_awaited()
        environment.control.delete.assert_not_awaited()
        environment.control.delete_service.assert_awaited_once_with(
            environment.vm_name, owner=environment.token[:12]
        )

    async def test_service_delete_failure_is_retried_after_vm_is_gone(self):
        import httpx

        environment = self.environment()
        environment._created = True
        state = {"vm": True, "service": True, "service_deletes": 0}
        calls = []

        def handler(request):
            path = request.url.path
            calls.append((request.method, path))
            if "/services/" in path:
                if request.method == "GET":
                    return httpx.Response(200, json={"metadata": {
                        "uid": "service-uid",
                        "labels": {OWNER_LABEL: environment.token[:12]},
                    }})
                state["service_deletes"] += 1
                if state["service_deletes"] <= 2:
                    return httpx.Response(500, text="transient failure")
                self.assertEqual(json.loads(request.content), {
                    "preconditions": {"uid": "service-uid"},
                })
                state["service"] = False
                return httpx.Response(200)
            if request.method == "DELETE":
                state["vm"] = False
                return httpx.Response(200)
            if request.method == "PUT":
                return httpx.Response(200)
            if "/virtualmachineinstances/" in path or not state["vm"]:
                return httpx.Response(404)
            return httpx.Response(200, json={"metadata": {
                "uid": "vm-uid", "labels": {OWNER_LABEL: environment.token[:12]},
            }})

        for attempt in range(3):
            # stop() closes its client, so replace only the mocked HTTP boundary.
            environment.control._client = httpx.AsyncClient(
                base_url="https://cluster.example", transport=httpx.MockTransport(handler),
                trust_env=False,
            )
            if attempt < 2:
                with self.assertRaisesRegex(RuntimeError, "Service delete failed"):
                    await environment.stop()
                self.assertTrue(environment._created)
            else:
                await environment.stop()
        self.assertEqual(state, {"vm": False, "service": False, "service_deletes": 3})
        self.assertFalse(environment._created)
        self.assertFalse(environment._create_attempted)
        self.assertEqual(sum(method == "PUT" for method, _ in calls), 1)
        self.assertEqual(sum(
            method == "DELETE" and "/virtualmachines/" in path for method, path in calls
        ), 1)
        count = len(calls)
        await environment.stop()
        self.assertEqual(len(calls), count)

    async def test_filtered_download_and_protected_reward(self):
        environment = self.environment()
        environment._started = True
        environment.transport = AsyncMock()
        environment.transport.list_files.return_value = [
            "reward.txt",
            "a.log",
            "private.log",
            "nested/b.log",
        ]
        await environment.download_dir_filtered(
            source_dir="C:/logs",
            target_dir=self.root / "out",
            include=["*.log"],
            exclude=["private*"],
            protect=["reward.txt"],
        )
        sources = [
            call.args[0] for call in environment.transport.download_file.call_args_list
        ]
        self.assertEqual(
            sources, ["C:/logs/a.log", "C:/logs/nested/b.log", "C:/logs/reward.txt"]
        )

    def running_environment(self, files):
        environment = self.environment()
        environment._created = environment._started = True
        environment.control = AsyncMock()
        environment.control.get.return_value = {"labels": {OWNER_LABEL: environment.token[:12]}}
        environment.transport = AsyncMock()

        async def list_files(source):
            prefix = source + "/"
            return [path[len(prefix):] for path in files if path.startswith(prefix)]

        async def download(source, target):
            environment.control.stop.assert_not_awaited()
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[source])

        environment.transport.list_files.side_effect = list_files
        environment.transport.download_file.side_effect = download
        return environment

    async def test_log_download_after_stop_uses_snapshot_and_preserves_host_outputs(self):
        for delete in (True, False):
            with self.subTest(delete=delete):
                environment = self.running_environment({
                    "C:/logs/agent/agent.log": "guest agent",
                    "C:/logs/agent/private.log": "private",
                    "C:/logs/agent/nested/debug.log": "debug",
                    "C:/logs/verifier/reward.txt": "0",
                    "C:/logs/artifacts/result.txt": "artifact",
                })
                # The guest transport's own temporary directory is removed by stop().
                environment._local_dir = tempfile.TemporaryDirectory()
                agent_dir = self.root / f"output-{delete}" / "agent"
                (agent_dir / "native").mkdir(parents=True)
                (agent_dir / "native/traj.jsonl").write_text("host trajectory")
                await environment.stop(delete=delete)
                self.assertFalse(environment._started)
                self.assertIsNone(environment._local_dir)
                environment.control.stop.assert_awaited_once()
                self.assertEqual(environment.control.delete.await_count, int(delete))
                environment.transport.close.assert_awaited_once()
                downloads = environment.transport.download_file.await_count
                await environment.download_dir_filtered(
                    source_dir="C:/logs/agent", target_dir=agent_dir,
                    include=["*.log"], exclude=["private*"],
                )
                await environment.download_dir_filtered(
                    source_dir="C:/logs/verifier", target_dir=self.root / "verifier",
                    exclude=["*"], protect=["reward.txt"],
                )
                await environment.download_dir("C:/logs/artifacts", self.root / "artifacts")
                await environment.download_file("C:/logs/agent/nested/debug.log", self.root / "single.log")
                self.assertEqual((agent_dir / "agent.log").read_text(), "guest agent")
                self.assertEqual((agent_dir / "nested/debug.log").read_text(), "debug")
                self.assertFalse((agent_dir / "private.log").exists())
                self.assertEqual((agent_dir / "native/traj.jsonl").read_text(), "host trajectory")
                self.assertEqual((self.root / "verifier/reward.txt").read_text(), "0")
                self.assertEqual((self.root / "artifacts/result.txt").read_text(), "artifact")
                self.assertEqual((self.root / "single.log").read_text(), "debug")
                await environment.stop(delete=delete)
                self.assertEqual(environment.transport.download_file.await_count, downloads)

    async def test_snapshot_failure_keeps_completed_files_and_does_not_block_cleanup(self):
        environment = self.running_environment({
            "C:/logs/agent/complete.log": "complete",
            "C:/logs/agent/interrupted.log": "partial",
            "C:/logs/artifacts/result.txt": "artifact",
        })
        download = environment.transport.download_file.side_effect

        async def fail_partial(source, target):
            await download(source, target)
            if source.endswith("interrupted.log"):
                raise RuntimeError("guest disconnected")

        environment.transport.download_file.side_effect = fail_partial
        with self.assertLogs(environment.logger, level="ERROR"):
            await environment.stop()
        environment.control.delete.assert_awaited_once()
        await environment.download_dir("C:/logs/agent", self.root / "saved")
        self.assertEqual((self.root / "saved/complete.log").read_text(), "complete")
        self.assertFalse((self.root / "saved/interrupted.log").exists())
        await environment.download_dir("C:/logs/artifacts", self.root / "artifacts")
        self.assertEqual((self.root / "artifacts/result.txt").read_text(), "artifact")

    async def test_snapshot_timeout_does_not_block_vm_cleanup(self):
        environment = self.running_environment({})
        environment.settings = replace(environment.settings, transfer_timeout=0.01)

        async def hang(*_):
            await asyncio.Event().wait()

        environment.transport.list_files.side_effect = hang
        with self.assertLogs(environment.logger, level="WARNING"):
            await asyncio.wait_for(environment.stop(), timeout=1)
        environment.control.stop.assert_awaited_once()
        environment.control.delete.assert_awaited_once()
        environment.transport.close.assert_awaited_once()

    async def test_cancellation_during_snapshot_still_deletes_vm(self):
        environment = self.running_environment({})
        environment.transport.list_files.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await environment.stop()
        environment.control.stop.assert_awaited_once()
        environment.control.delete.assert_awaited_once()
        environment.transport.close.assert_awaited_once()

    async def test_snapshot_disk_error_does_not_block_vm_cleanup(self):
        environment = self.running_environment({})
        with patch('kubevirt_windows.environment.tempfile.TemporaryDirectory', side_effect=OSError('disk full')), \
                self.assertLogs(environment.logger, level='ERROR'):
            await environment.stop()
        environment.control.stop.assert_awaited_once()
        environment.control.delete.assert_awaited_once()

    async def test_cached_download_still_rejects_host_symlink_escape(self):
        environment = self.running_environment({"C:/logs/agent/nested/debug.log": "debug"})
        await environment.stop()
        target = self.root / "out"
        target.mkdir()
        (target / "nested").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escape"):
            await environment.download_dir("C:/logs/agent", target)
        self.assertFalse((self.root / "debug.log").exists())

    async def test_download_cannot_follow_host_symlink(self):
        environment = self.environment()
        environment._started = True
        environment.transport = AsyncMock()
        environment.transport.list_files.return_value = ["escape/file"]
        target = self.root / "out"
        target.mkdir()
        (target / "escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escape"):
            await environment.download_dir("C:/logs", target)
        environment.transport.download_file.assert_not_awaited()

    async def test_agent_passes_instruction_as_data_and_records_exit_code(self):
        environment = self.environment()
        environment._started = True
        environment.transport = AsyncMock()
        environment.transport.execute.return_value = {
            "stdout": "",
            "stderr": "",
            "return_code": 0,
        }
        captured = {}

        async def upload(source, target):
            captured[target] = Path(source).read_text()

        environment.transport.upload_file.side_effect = upload
        agent = WindowsCommandAgent(
            logs_dir=self.root / "logs",
            command="C:\\Agent\\run.cmd",
            model_name="example/model",
        )
        context = AgentContext()
        instruction = 'Run this: %PATH% & echo "任务"'
        await agent.setup(environment)
        await agent.run(instruction, environment, context)
        self.assertEqual(captured["C:/logs/agent/instruction.txt"], instruction)
        self.assertNotIn(instruction, environment.transport.execute.call_args.args[0])
        self.assertEqual(
            environment.transport.execute.call_args.kwargs["env"]["HARBOR_MODEL"],
            "example/model",
        )
        self.assertEqual(context.metadata, {"exit_code": 0})

    async def test_agent_runtime_env_overrides_preparation_defaults(self):
        environment = self.environment()
        environment._started = True
        environment.transport = AsyncMock()
        environment.transport.execute.return_value = {
            "stdout": "", "stderr": "", "return_code": 0,
        }
        manifest = self.root / "prepare.json"
        manifest.write_text(json.dumps({
            "version": 1, "env": {"TOOL_HOME": "C:/tools/default"},
        }))
        agent = WindowsCommandAgent(
            logs_dir=self.root / "logs", command="tool.exe",
            check_command="tool.exe --version", prepare_manifest=manifest,
        )
        with environment.scoped_exec_env({"TOOL_HOME": "C:/tools/override"}):
            await agent.setup(environment)
            await agent.run("task", environment, AgentContext())
        for call in environment.transport.execute.await_args_list:
            self.assertEqual(call.kwargs["env"]["TOOL_HOME"], "C:/tools/override")


class LauncherTests(unittest.TestCase):
    def test_launch_preserves_arguments_and_opik_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "calls.jsonl"
            script = (
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "with open(os.environ['TEST_CAPTURE'], 'a') as f:\n"
                "    f.write(json.dumps({'exe': Path(sys.argv[0]).name, 'args': sys.argv[1:], "
                "'pythonpath': os.environ.get('PYTHONPATH')}) + '\\n')\n"
            )
            for name in ("python", "harbor", "opik"):
                executable = bin_dir / name
                executable.write_text(script)
                executable.chmod(0o755)
            for endpoint, executable in [
                ("", "harbor"),
                ("https://opik.example/api", "opik"),
            ]:
                with self.subTest(endpoint=endpoint):
                    capture.unlink(missing_ok=True)
                    result = subprocess.run(
                        [
                            "bash",
                            str(HARBOR_DIR / "run_kubevirt_windows.sh"),
                            "--path",
                            "/external/task with spaces",
                            "--agent",
                            "oracle",
                        ],
                        env={
                            **os.environ,
                            "HARBOR_RUNNER_DIR": str(root),
                            "HARBOR_OPIK_PYTHON": str(bin_dir / "python"),
                            "HARBOR_CLI_BIN": str(bin_dir / "harbor"),
                            "HARBOR_OPIK_BIN": str(bin_dir / "opik"),
                            "OPIK_URL": endpoint,
                            "TEST_CAPTURE": str(capture),
                        },
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [
                        json.loads(line) for line in capture.read_text().splitlines()
                    ]
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(calls[0]["args"][-1], "--validate")
                    self.assertIn("preflight()", calls[1]["args"][-1])
                    self.assertEqual(calls[2]["exe"], executable)
                    self.assertIn("/external/task with spaces", calls[2]["args"])
                    self.assertEqual(
                        calls[2]["args"][-2:],
                        [
                            "--env",
                            "kubevirt_windows.environment:KubeVirtWindowsEnvironment",
                        ],
                    )
                    self.assertEqual(calls[2]["args"][-3], "oracle")
                    self.assertTrue(calls[2]["pythonpath"].startswith(str(HARBOR_DIR)))

    def test_dry_run_never_calls_cluster_tools_or_prints_secrets(self):
        result = subprocess.run(
            [
                "bash",
                str(HARBOR_DIR / "run_kubevirt_windows.sh"),
                "--dry-run",
                "--path",
                "/external/tasks",
                "--ae",
                "KEY=fake-secret",
            ],
            env={**os.environ, "HARBOR_OPIK_PYTHON": "/missing/python", "OPIK_URL": ""},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kubevirt_windows.environment", result.stdout)
        self.assertNotIn("fake-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
