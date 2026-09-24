"""Harbor environment backed by a fresh Windows VM for each trial."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS
from harbor.utils.path_filter import filter_paths_by_patterns

from .control import (
    DEFAULT_DISK_SIZE,
    OWNER_LABEL,
    PlatformAPIError,
    PlatformControl,
    Settings,
    _api_base,
    pick_root_disk_size,
    raise_for_platform,
)
from .transport import WindowsSSH, windows_path


class KubeVirtWindowsEnvironment(BaseEnvironment):
    def __init__(self, *args, **kwargs):
        self.settings = Settings.from_env()
        self.token = uuid.uuid4().hex
        self.vm_name = "hf-win-" + self.token[:24]
        self.control = PlatformControl(self.settings)
        self._created = False
        self._local_dir = None
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
        for executable in ("ssh", "sftp"):
            if not shutil.which(executable):
                raise ValueError(f"{executable} is required on the Linux runner")
        # Read-only reachability + token check; never mutates the platform.
        try:
            import httpx

            with httpx.Client(
                base_url=_api_base(settings.platform.base_url),
                headers={"Authorization": f"Bearer {settings.platform.token}"},
                timeout=httpx.Timeout(15.0),
            ) as client:
                raise_for_platform(client.get("/users/me"))
        except ValueError:
            raise
        except httpx.HTTPError as exc:
            raise ValueError(
                f"KubeVirt platform unreachable at {settings.platform.base_url}: {exc}"
            ) from exc
        except PlatformAPIError as exc:
            raise ValueError(
                f"KubeVirt platform rejected the token: {exc}"
            ) from exc

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
        # via SFTP; arbitrary host mounts cannot be implemented by a remote VM.
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
        if self._created:
            raise RuntimeError(
                "A retained VM cannot be reused; create a new environment instance"
            )
        self._local_dir = tempfile.TemporaryDirectory(prefix="harbor-kubevirt-")
        tag = self.token[:12]
        ips = await self.control.available_ips()
        ip = ips[0] if ips else None
        if not ip:
            raise RuntimeError("No free IP available on subnet " + self.settings.subnet)
        self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.trial_dir / "kubevirt.json").write_text(
            json.dumps(
                {
                    "vm": self.vm_name,
                    "namespace": self.settings.namespace,
                    "subnet": self.settings.subnet,
                    "ip": ip,
                    "labels": {OWNER_LABEL: tag},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            async with asyncio.timeout(self.settings.start_timeout):
                # Disk must be >= the source template's minSize to clone.
                min_size = await self.control.image_min_size(self.settings.image)
                disk_size = pick_root_disk_size(DEFAULT_DISK_SIZE, min_size)
                await self.control.create(
                    self.vm_name,
                    ip,
                    labels={OWNER_LABEL: tag},
                    disk_size=disk_size,
                )
                self._created = True
                # Create leaves the VM defined/Stopped; power it on explicitly.
                await self.control.start(self.vm_name)
                while True:
                    vm = await self.control.get(self.vm_name)
                    if vm.get("ready") and vm.get("ip"):
                        ip = vm["ip"]
                        break
                    await asyncio.sleep(2)
                self.transport = WindowsSSH(
                    self.settings, self.vm_name, ip, self._local_dir.name
                )
                while True:
                    try:
                        await self.transport.probe()
                        break
                    except (RuntimeError, TimeoutError):
                        # VM readiness precedes Windows/OpenSSH readiness.
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
            try:
                await self.control.stop(self.vm_name)
            finally:
                if delete:
                    await self.control.delete(self.vm_name)
        finally:
            self._started = False
            self._created = False
            if self._local_dir is not None:
                self._local_dir.cleanup()
                self._local_dir = None
            await self.control.close()

    def _guest(self):
        if not self._started or self.transport is None:
            raise RuntimeError("Windows VM has not started")
        return self.transport

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        effective_user = user if user is not None else self.default_user
        if (
            effective_user is not None
            and str(effective_user).lower() != self.settings.ssh_user.lower()
        ):
            raise ValueError("Windows commands must run as the configured SSH user")
        timeout = self.settings.command_timeout if timeout_sec is None else timeout_sec
        if timeout <= 0:
            raise ValueError("Command timeout must be positive")
        merged = self._merge_env(env) or {}
        for key, value in merged.items():
            if not key or any(c in key for c in "=\x00") or "\x00" in value:
                raise ValueError("Invalid Windows environment variable")
        result = await self._guest().execute(
            command,
            cwd=cwd or self.task_env_config.workdir or "C:/workspace",
            env=merged,
            timeout=timeout,
        )
        output = ExecResult(
            stdout=result["stdout"],
            stderr=result["stderr"],
            return_code=result["return_code"],
        )
        callback = self._output_callback()
        if callback:
            for stream in ("stdout", "stderr"):
                if getattr(output, stream):
                    await callback(getattr(output, stream), stream)
        return output

    async def upload_file(self, source_path, target_path):
        await self._guest().upload_file(source_path, target_path)

    async def upload_dir(self, source_dir, target_dir):
        await self._guest().upload_dir(source_dir, target_dir)

    async def download_file(self, source_path, target_path):
        await self._guest().download_file(source_path, target_path)

    async def download_dir(self, source_dir, target_dir):
        await self.download_dir_filtered(source_dir=source_dir, target_dir=target_dir)

    async def download_dir_with_exclusions(self, *, source_dir, target_dir, exclude):
        await self.download_dir_filtered(
            source_dir=source_dir, target_dir=target_dir, exclude=exclude
        )

    async def download_dir_filtered(
        self, *, source_dir, target_dir, include=None, exclude=None, protect=None
    ):
        paths = await self._guest().list_files(source_dir)
        selected = filter_paths_by_patterns(paths, include=include, exclude=exclude)
        selected = list(
            dict.fromkeys(selected + [p for p in paths if p in (protect or [])])
        )
        target = Path(target_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        for path in selected:
            local = target / path
            if not local.resolve().is_relative_to(target):
                raise ValueError("Download would escape the output directory")
            await self.download_file(
                windows_path(source_dir).rstrip("/") + "/" + path, local
            )
