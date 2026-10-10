"""Harbor environment backed by a fresh Windows VM for each trial."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid

import httpx
from harbor.environments.base import BaseEnvironment
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS
from windows_guest import WindowsGuestIO

from .ale import ALETransport
from .control import (
    OWNER_LABEL,
    KubeVirtControl,
    PlatformAPIError,
    Settings,
)
from .transport import WAATransport, windows_path


class KubeVirtWindowsEnvironment(WindowsGuestIO, BaseEnvironment):
    def __init__(self, *args, **kwargs):
        self.settings = Settings.from_env()
        self.token = uuid.uuid4().hex
        self.vm_name = "hf-win-" + self.token[:24]
        self.control = KubeVirtControl(self.settings)
        self._created = False
        self._create_attempted = False
        self._local_dir = None
        self._log_snapshot = None
        self.transport = None
        self._started = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def type():
        return "kubevirt-windows"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(windows=True)

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @classmethod
    def preflight(cls):
        settings = Settings.from_env()
        # Read-only reachability of the kube-apiserver; never mutates clusters.
        control = KubeVirtControl(settings)
        try:
            import asyncio

            async def check():
                ok = await control.ping()
                if not ok:
                    raise RuntimeError("KubeVirt apiserver not reachable or not authorized")

            asyncio.get_event_loop().run_until_complete(check())
        except ValueError:
            raise
        except (httpx.HTTPError, RuntimeError, OSError) as exc:
            raise ValueError(
                f"KubeVirt apiserver unreachable at {settings.cluster.api_server}: {exc}"
            ) from exc
        finally:
            asyncio.get_event_loop().run_until_complete(control.close())

    def _validate_definition(self):
        if self.os != TaskOS.WINDOWS:
            raise ValueError("KubeVirt Windows requires [environment].os = 'windows'")
        # Fail closed even for consumers constructing the environment directly
        # instead of through Harbor's trial network-policy resolver.
        if self.task_env_config.network_mode.value != "public":
            raise ValueError("KubeVirt Windows does not enforce task network policies")
        if self.task_env_config.docker_image:
            raise ValueError(
                "Select the Windows image in HARBOR_KUBEVIRT_IMAGE, not docker_image"
            )
        if self.task_env_config.storage_mb is not None:
            raise ValueError(
                "Configure disk capacity in the VM template; storage_mb is unsupported"
            )
        for filename in (
            "Dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        ):
            if (self.environment_dir / filename).exists():
                raise ValueError(
                    "KubeVirt Windows uses a prepared VM template, not Docker build files"
                )
        # Harbor always supplies these three log mount hints. We collect them
        # via guest HTTP; arbitrary host mounts cannot be implemented by a remote VM.
        logs = {"c:/logs/agent", "c:/logs/verifier", "c:/logs/artifacts"}
        for mount in self._mounts:
            if windows_path(mount["target"]).lower() not in logs or mount.get(
                "read_only"
            ):
                raise ValueError("Host bind mounts are unsupported by KubeVirt Windows")
        if self.task_env_config.workdir:
            windows_path(self.task_env_config.workdir)

    async def start(self, force_build=False):
        if self._started:
            return
        if self._created or self._create_attempted:
            raise RuntimeError(
                "A retained VM cannot be reused; create a new environment instance"
            )
        self._local_dir = tempfile.TemporaryDirectory(prefix="harbor-kubevirt-")
        tag = self.token[:12]
        try:
            async with asyncio.timeout(self.settings.start_timeout):
                self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
                (self.trial_paths.trial_dir / "kubevirt.json").write_text(
                    json.dumps(
                        {
                            "vm": self.vm_name,
                            "namespace": self.settings.namespace,
                            "source_pvc": self.settings.image,
                            "root_datavolume": self.vm_name + "-root",
                            "node": self.settings.node,
                            "guest_protocol": self.settings.guest_protocol,
                            "labels": {OWNER_LABEL: tag},
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self._create_attempted = True
                await self.control.create(
                    self.vm_name,
                    labels={OWNER_LABEL: tag},
                    cpus=self.task_env_config.cpus,
                    memory_mb=self.task_env_config.memory_mb,
                )
                self._created = True
                # Create leaves the VM defined/Stopped; power it on explicitly.
                await self.control.start(self.vm_name)
                while True:
                    vm = await self.control.get(self.vm_name)
                    if vm.get("ready") and vm.get("ip"):
                        break
                    await asyncio.sleep(2)
                # Expose the guest server on a node port reachable from the
                # runner and connect the guest transport to that endpoint.
                node_endpoint = await self.control.expose_guest(
                    self.vm_name, owner=tag
                )
                host, port = node_endpoint.rsplit(":", 1)
                transport_class = (
                    ALETransport if self.settings.guest_protocol == "ale" else WAATransport
                )
                self.transport = transport_class(
                    self.settings, self.vm_name, host, self._local_dir.name,
                    guest_port=int(port),
                )
                while True:
                    try:
                        await self.transport.probe()
                        break
                    except (RuntimeError, TimeoutError):
                        # VM readiness precedes guest service readiness.
                        await asyncio.sleep(2)
                await self.transport.prepare()
                for path in (
                    "C:/logs/agent",
                    "C:/logs/verifier",
                    "C:/logs/artifacts",
                    self.task_env_config.workdir or "C:/workspace",
                ):
                    await self.transport.mkdir(path)
                self._started = True
        except BaseException:
            try:
                await asyncio.shield(self.stop(delete=True))
            except BaseException:
                self.logger.exception(
                    "Failed to clean up VM %s after startup failure", self.vm_name
                )
            raise

    async def stop(self, delete=True):
        try:
            if not (self._created or self._create_attempted):
                return
            try:
                vm = await self.control.get(self.vm_name)
            except (httpx.HTTPStatusError, PlatformAPIError) as exc:
                code = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else exc.code
                )
                if code != 404:
                    raise
                if delete:
                    # VM deletion may have succeeded before Service deletion
                    # failed. Keep retry state until both resources are gone.
                    await self.control.delete_service(
                        self.vm_name, owner=self.token[:12]
                    )
                self._created = self._create_attempted = False
                return
            if vm.get("labels", {}).get(OWNER_LABEL) != self.token[:12]:
                raise RuntimeError(f"VM ownership mismatch: {self.vm_name}")
            try:
                # Startup failures can stop the VM before Harbor recovers logs.
                # Keep a separate snapshot alive until this environment is freed.
                try:
                    await self._preserve_logs()
                finally:
                    await self.control.stop(self.vm_name)
            finally:
                if delete:
                    await self.control.delete(self.vm_name, owner=self.token[:12])
                    self._created = self._create_attempted = False
        finally:
            self._started = False
            if self._local_dir is not None:
                self._local_dir.cleanup()
                self._local_dir = None
            try:
                if self.transport is not None:
                    await self.transport.close()
            finally:
                await self.control.close()
