"""Harbor Windows guest backed by a local Dockur container per trial."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid

from harbor.environments.base import BaseEnvironment
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS
from kubevirt_windows.transport import WAATransport, windows_path
from windows_guest import WindowsGuestIO

from .control import DockerControl, Settings


class DockerWindowsEnvironment(WindowsGuestIO, BaseEnvironment):
    def __init__(self, *args, **kwargs):
        self.settings = Settings.from_env()
        self.token = uuid.uuid4().hex
        self.vm_name = "hf-dockur-" + self.token[:24]
        self.control = DockerControl(self.settings)
        self.transport = self._local_dir = self._log_snapshot = None
        self._started = self._create_attempted = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def type():
        return "docker-windows"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(windows=True)

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    def _validate_definition(self):
        if self.os != TaskOS.WINDOWS or self.task_env_config.network_mode.value != "public":
            raise ValueError("Dockur requires Windows tasks with public network mode")
        if self.task_env_config.docker_image or self.task_env_config.storage_mb is not None:
            raise ValueError("Configure prepared Dockur image/storage using HARBOR_WAA_DOCKER_* settings")
        if any((self.environment_dir / name).exists() for name in
               ("Dockerfile", "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")):
            raise ValueError("Dockur uses a prepared Windows disk; task builds are unsupported")
        for mount in self._mounts:
            if windows_path(mount["target"]).lower() not in {"c:/logs/agent", "c:/logs/verifier", "c:/logs/artifacts"} or mount.get("read_only"):
                raise ValueError("Dockur task mounts support only Harbor's guest log directories")
        if self.task_env_config.workdir:
            windows_path(self.task_env_config.workdir)

    async def start(self, force_build=False):
        if self._started:
            return
        if self._create_attempted:
            raise RuntimeError("A retained Dockur trial cannot restart; construct a fresh environment")
        try:
            async with asyncio.timeout(self.settings.start_timeout):
                await self.control.preflight()
                self._local_dir = tempfile.TemporaryDirectory(prefix="harbor-dockur-")
                self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
                metadata = {"container": self.vm_name, "owner": self.token[:12],
                            "image": self.settings.image, "golden_storage": str(self.settings.storage),
                            "trial_storage": str(self.control.instance(self.vm_name) / "storage")}
                (self.trial_paths.trial_dir / "docker-windows.json").write_text(json.dumps(metadata, indent=2) + "\n")
                self._create_attempted = True
                await self.control.create(self.vm_name, owner=self.token[:12],
                                          cpus=self.task_env_config.cpus, memory_mb=self.task_env_config.memory_mb)
                await self.control.start(self.vm_name)
                endpoints = await self.control.guest_endpoints(self.vm_name, owner=self.token[:12])
                metadata["endpoints"] = endpoints
                (self.trial_paths.trial_dir / "docker-windows.json").write_text(json.dumps(metadata, indent=2) + "\n")
                from urllib.parse import urlsplit

                address = urlsplit(endpoints[5000])
                self.transport = WAATransport(self.settings, self.vm_name, address.hostname,
                                              self._local_dir.name, guest_port=address.port)
                while True:
                    try:
                        await self.transport.probe()
                        break
                    except (RuntimeError, TimeoutError):
                        await self.control.guest_endpoints(self.vm_name, owner=self.token[:12])
                        await asyncio.sleep(2)
                await self.transport.prepare()
                for path in ("C:/logs/agent", "C:/logs/verifier", "C:/logs/artifacts",
                             self.task_env_config.workdir or "C:/workspace"):
                    await self.transport.mkdir(path)
                self._started = True
        except BaseException:
            try:
                await asyncio.shield(self.stop(delete=True))
            except BaseException:
                self.logger.exception("Failed to clean up Dockur trial %s", self.vm_name)
            raise

    async def stop(self, delete=True):
        try:
            if self._create_attempted:
                try:
                    await self._preserve_logs()
                    await self.control.logs(self.vm_name, self.trial_paths.trial_dir / "docker.log")
                finally:
                    await self.control.delete(self.vm_name, owner=self.token[:12], delete=delete)
                    if delete:
                        self._create_attempted = False
        finally:
            self._started = False
            if self._local_dir is not None:
                self._local_dir.cleanup()
                self._local_dir = None
            if self.transport is not None:
                await self.transport.close()
