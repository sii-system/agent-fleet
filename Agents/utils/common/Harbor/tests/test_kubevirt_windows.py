"""Offline tests against Harbor's real Windows environment/agent interfaces."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from kubevirt_windows.agent import WindowsCommandAgent
from kubevirt_windows.control import (
    Platform,
    Settings,
)
from kubevirt_windows.environment import KubeVirtWindowsEnvironment
from kubevirt_windows.transport import WindowsSSH, ps_quote, sftp_quote, windows_path


def make_settings(key: Path) -> Settings:
    return Settings(
        platform=Platform(base_url="http://10.9.202.91:31600", token="tok"),
        image="ubuntu20.04-template-image",
        namespace="default",
        ssh_user="runner",
        ssh_key=key,
        subnet="ovn-default",
        storage_class="ceph-rbd-sc",
    )


class PathTests(unittest.TestCase):
    def test_windows_paths_and_quoting(self):
        self.assertEqual(windows_path(r"C:\任务\a b.txt"), "C:/任务/a b.txt")
        self.assertEqual(ps_quote("a'b"), "'a''b'")
        self.assertEqual(sftp_quote("a b[1].txt"), '"a b\\[1].txt"')

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
        key = root / "id_rsa"
        key.write_text("fake-key")
        self.settings = make_settings(key)
        self.transport = WindowsSSH(
            self.settings, "trial", "10.9.202.100", root / "known-hosts"
        )

    async def test_request_uses_file_not_command_interpolation(self):
        captured = {}

        async def upload(source, target):
            captured.update(json.loads(Path(source).read_text()))

        self.transport.upload_file = AsyncMock(side_effect=upload)
        self.transport.powershell = AsyncMock(
            return_value='{"stdout":"ok","stderr":"","return_code":7,"timed_out":false}'
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
        self.transport.powershell = AsyncMock(return_value='{"timed_out":true}')
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
        key = self.root / "key"
        key.write_text("fake-key")
        self.settings = make_settings(key)
        patcher = patch.object(Settings, "from_env", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def environment(self, **config):
        return KubeVirtWindowsEnvironment(
            environment_dir=self.root / "environment",
            environment_name="windows-test",
            session_id="test-trial",
            trial_paths=TrialPaths(self.root / "trial"),
            task_env_config=EnvironmentConfig(os="windows", **config),
        )

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
        with self.assertRaisesRegex(ValueError, "SSH user"):
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
        environment = self.environment()
        environment.control.available_ips = AsyncMock(return_value=["10.9.202.100"])
        environment.control.image_min_size = AsyncMock(return_value="40Gi")
        environment.control.create = AsyncMock()
        environment.control.start = AsyncMock()
        environment.control.get = AsyncMock(
            return_value={
                "name": environment.vm_name,
                "namespace": "default",
                "ip": "10.9.202.100",
                "ready": True,
                "labels": {},
                "status": "Running",
                "uid": "x",
            }
        )
        with patch("kubevirt_windows.environment.WindowsSSH") as transport_class:
            transport_class.return_value = AsyncMock()
            await environment.start()
        self.assertTrue(environment._started)
        environment.control.image_min_size.assert_awaited_once_with(
            environment.settings.image
        )
        environment.control.create.assert_awaited_once()
        _, kwargs = environment.control.create.await_args
        self.assertEqual(kwargs["disk_size"], "40Gi")
        environment.control.start.assert_awaited_once_with(environment.vm_name)
        environment.transport.probe.assert_awaited_once()
        self.assertEqual(environment.transport.mkdir.await_count, 4)
        metadata = json.loads((self.root / "trial/kubevirt.json").read_text())
        self.assertEqual(metadata["vm"], environment.vm_name)
        self.assertEqual(metadata["ip"], "10.9.202.100")
        self.assertNotIn("fake-key", json.dumps(metadata))
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        await environment.stop()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(environment.vm_name)

    async def test_failed_start_cleans_up(self):
        environment = self.environment()
        environment.control.available_ips = AsyncMock(return_value=["10.9.202.100"])
        environment.control.image_min_size = AsyncMock(return_value="40Gi")
        environment.control.create = AsyncMock(
            side_effect=RuntimeError("creation failed")
        )
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "creation failed"):
            await environment.start()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(environment.vm_name)
        environment.control.close.assert_awaited_once()
        self.assertIsNone(environment._local_dir)

    async def test_cancelled_start_cleans_up(self):
        environment = self.environment()
        environment.control.available_ips = AsyncMock(return_value=["10.9.202.100"])
        environment.control.image_min_size = AsyncMock(return_value="40Gi")
        environment.control.create = AsyncMock(side_effect=asyncio.CancelledError)
        environment.control.stop = AsyncMock()
        environment.control.delete = AsyncMock()
        environment.control.close = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await environment.start()
        environment.control.stop.assert_awaited_once_with(environment.vm_name)
        environment.control.delete.assert_awaited_once_with(environment.vm_name)
        environment.control.close.assert_awaited_once()

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