"""Dockur storage isolation, ownership, failure recovery and local-host checks."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docker_windows import control
from docker_windows.control import OWNER_LABEL, DockerControl, Settings
from docker_windows.environment import DockerWindowsEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "golden"
        self.storage.mkdir()
        (self.storage / "data.img").write_bytes(b"golden")
        values = {"HARBOR_WAA_DOCKER_STORAGE": str(self.storage),
                  "HARBOR_WAA_DOCKER_INSTANCES": str(self.root / "trials")}
        patcher = patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)



class SettingsTests(Fixture, unittest.TestCase):
    def test_requires_separate_storage_and_positive_timeouts(self):
        with patch.dict(os.environ, {"HARBOR_WAA_DOCKER_STORAGE": ""}), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {"HARBOR_WAA_DOCKER_INSTANCES": str(self.storage / "trials")}), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {"HARBOR_WAA_DOCKER_START_TIMEOUT": "0"}), self.assertRaises(ValueError):
            Settings.from_env()

    def test_rejects_symlinks_and_qcow2_backing_files(self):
        (self.storage / "linked").symlink_to(self.storage / "data.img")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            Settings.from_env()
        (self.storage / "linked").unlink()
        (self.storage / "data.img").write_bytes(b"QFI\xfb" + struct.pack(">IQI", 3, 72, 10))
        with self.assertRaisesRegex(ValueError, "backing files"):
            Settings.from_env()

    def test_qcow2_format_and_boot_hardware_settings(self):
        (self.storage / "data.img").unlink()
        (self.storage / "data.qcow2").write_bytes(b"QFI\xfb" + struct.pack(">IQI", 3, 0, 0))
        with patch.dict(os.environ, {"HARBOR_WAA_DOCKER_DISK_TYPE": "scsi"}):
            settings = Settings.from_env()
        self.assertEqual(dict(settings.boot_env), {"DISK_FMT": "qcow2", "DISK_TYPE": "scsi"})


class ControlTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_trial_disk_is_independent_and_cleanup_keeps_golden(self):
        settings = Settings.from_env()
        backend = DockerControl(settings)
        actual_run = control.run
        calls = []

        async def execute(*args, **kwargs):
            calls.append(args)
            if args[0] == "cp":
                return await actual_run(*args, **kwargs)
            return "id"

        with patch.object(control, "run", side_effect=execute):
            await backend.create("hf-dockur-one", owner="owner", cpus=2, memory_mb=4096)
        cloned = backend.instance("hf-dockur-one") / "storage/data.img"
        self.assertNotEqual(cloned.stat().st_ino, (self.storage / "data.img").stat().st_ino)
        cloned.write_bytes(b"changed")
        self.assertEqual((self.storage / "data.img").read_bytes(), b"golden")
        create = calls[-1]
        self.assertIn("127.0.0.1::9222/tcp", create)
        self.assertIn("127.0.0.1::8080/tcp", create)
        self.assertIn("CPU_CORES=2", create)
        self.assertNotIn("--privileged", create)
        with patch.object(backend, "inspect", AsyncMock(return_value=None)):
            await backend.delete("hf-dockur-one", owner="owner")
        self.assertFalse(cloned.parent.exists())
        self.assertEqual((self.storage / "data.img").read_bytes(), b"golden")

    async def test_cleanup_refuses_wrong_container_or_storage_owner(self):
        backend = DockerControl(Settings.from_env())
        root = backend.instance("hf-dockur-one")
        root.mkdir(parents=True)
        (root / "owner").write_text("another")
        with patch.object(backend, "inspect", AsyncMock(return_value=None)), self.assertRaisesRegex(RuntimeError, "storage ownership"):
            await backend.delete("hf-dockur-one", owner="owner")
        self.assertTrue(root.exists())
        value = [{"Config": {"Labels": {OWNER_LABEL: "another"}}}]
        with patch.object(control, "run", AsyncMock(return_value=json.dumps(value))), self.assertRaisesRegex(RuntimeError, "container ownership"):
            await backend.inspect("hf-dockur-one", owner="owner")

    async def test_remote_daemon_and_running_golden_are_rejected(self):
        backend = DockerControl(Settings.from_env())
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375"}), self.assertRaisesRegex(ValueError, "local Docker"):
            await backend.preflight()
        values = [json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]),
                  json.dumps({"OSType": "linux", "SecurityOptions": []}), "image", "id",
                  json.dumps([{"Mounts": [{"Type": "bind", "RW": True, "Source": str(self.storage)}]}])]
        with patch.object(control, "run", AsyncMock(side_effect=values)), patch.object(Path, "exists", return_value=True), \
                self.assertRaisesRegex(ValueError, "running container"):
            await backend.preflight()

    async def test_failed_copy_can_be_cleaned_without_a_container(self):
        backend = DockerControl(Settings.from_env())
        with patch.object(control, "run", AsyncMock(side_effect=asyncio.CancelledError())), self.assertRaises(asyncio.CancelledError):
            await backend.create("hf-dockur-one", owner="owner", cpus=2, memory_mb=4096)
        with patch.object(backend, "inspect", AsyncMock(return_value=None)):
            await backend.delete("hf-dockur-one", owner="owner")
        self.assertFalse(backend.instance("hf-dockur-one").exists())

    async def test_retained_trial_keeps_storage_and_stops_container(self):
        backend = DockerControl(Settings.from_env())
        root = backend.instance("hf-dockur-one")
        root.mkdir(parents=True)
        (root / "owner").write_text("owner")
        (root / "storage").mkdir()
        with patch.object(backend, "inspect", AsyncMock(return_value={"State": {"Running": True}})), \
                patch.object(control, "run", AsyncMock()) as execute:
            await backend.delete("hf-dockur-one", owner="owner", delete=False)
        self.assertEqual(execute.call_args.args[:2], ("docker", "stop"))
        self.assertTrue((root / "storage").exists())

    async def test_root_owned_storage_cleanup_is_confined_and_retryable(self):
        backend = DockerControl(Settings.from_env())
        root = backend.instance("hf-dockur-one")
        root.mkdir(parents=True)
        (root / "owner").write_text("owner")
        (root / "storage").mkdir()
        # A failed privileged cleanup must preserve the marker for a retry.
        with patch.object(backend, "inspect", AsyncMock(return_value=None)), \
                patch.object(control.shutil, "rmtree", side_effect=PermissionError()), \
                patch.object(control, "run", AsyncMock(side_effect=RuntimeError("helper failed"))), \
                self.assertRaisesRegex(RuntimeError, "helper failed"):
            await backend.delete("hf-dockur-one", owner="owner")
        self.assertEqual((root / "owner").read_text(), "owner")
        real_rmtree = control.shutil.rmtree

        async def helper(*args, **kwargs):
            self.assertIn(f"type=bind,src={root},dst=/trial", args)
            self.assertEqual(args[-1], "rm -rf /trial/storage /trial/shared")
            self.assertEqual((root / "owner").read_text(), "owner")
            real_rmtree(root / "storage")

        with patch.object(backend, "inspect", AsyncMock(return_value=None)), \
                patch.object(control.shutil, "rmtree", side_effect=PermissionError()), \
                patch.object(control, "run", side_effect=helper):
            await backend.delete("hf-dockur-one", owner="owner")
        self.assertFalse(root.exists())
        self.assertEqual((self.storage / "data.img").read_bytes(), b"golden")


class EnvironmentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def environment(self, **kwargs):
        env = DockerWindowsEnvironment(environment_dir=self.root / "environment", environment_name="test",
                session_id="trial", trial_paths=TrialPaths(self.root / "trial"),
                task_env_config=EnvironmentConfig(os="windows", **kwargs))
        self.addCleanup(lambda: env._log_snapshot.cleanup() if env._log_snapshot else None)
        return env

    async def test_start_failure_and_log_failure_still_clean_up(self):
        env = self.environment()
        env.control = AsyncMock()
        env.control.instance = lambda name: self.root / name
        env.control.create.side_effect = RuntimeError("create failed")
        env.control.logs.side_effect = RuntimeError("logs failed")
        with self.assertRaisesRegex(RuntimeError, "create failed"):
            await env.start()
        env.control.delete.assert_awaited_once_with(env.vm_name, owner=env.token[:12], delete=True)
        self.assertFalse(env._create_attempted)

    async def test_network_policy_and_task_images_fail_closed(self):
        for kwargs in ({"network_mode": "none"}, {"docker_image": "example/windows:fake"}, {"storage_mb": 1}):
            with self.assertRaises(ValueError):
                self.environment(**kwargs)


if __name__ == "__main__":
    unittest.main()
