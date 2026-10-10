"""Harbor environment for native ALE Linux and Windows CPU sandboxes."""

import asyncio
import json
import os
import signal
from dataclasses import replace
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
)
from harbor.models.task.config import TaskOS
from kubevirt_windows.control import validate_image
from kubevirt_windows.environment import KubeVirtWindowsEnvironment

from .cua_proxy import CUAProxy
from .linux import DEFAULT_IMAGE, cleanup_sbx_attempt, create_backend, sbx_configured
from .source import REVISION, select_image, source_digest, validate_source


class ALEEnvironment(BaseEnvironment):
    def __init__(self, *args, source, native_python, image_map,
                 task_data_source="baked_in_sandbox", linux_backend="auto", **kwargs):
        self.source = str(validate_source(source))
        self.native_python = str(Path(native_python).absolute())  # Preserve the virtualenv symlink.
        if not Path(self.native_python).is_file():
            raise ValueError("Run ALE setup before benchmark launch")
        if task_data_source != "baked_in_sandbox" and not task_data_source.startswith(("gs://", "s3://", "oss://")):
            raise ValueError("ALE supports baked data and native gs/s3/oss staging")
        if linux_backend not in ("auto", "sbx", "docker"):
            raise ValueError("ALE Linux backend must be auto, sbx or docker")
        self.linux_backend, self.backend_kwargs = linux_backend, kwargs
        self.worker, self.backend, self.proxy = None, None, None
        self._started = False
        self._retained = False
        self._phase_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)
        self.native = json.loads((self.environment_dir / "ale.json").read_text())
        if self.native["revision"] != REVISION or self.native["source_sha256"] != source_digest(self.source):
            raise ValueError("ALE source changed since task conversion")
        if self.native["os"] != self.os.value:
            raise ValueError("Native ALE task OS differs from its Harbor environment")
        relative = Path(self.native["task"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "tasks":
            raise ValueError("Invalid native ALE task path")
        profile = select_image(self.native, json.loads(Path(image_map).read_text()))
        self.spec = {**self.native, "source": self.source, "profile": profile, "task_data_source": task_data_source}
        if self.os == TaskOS.WINDOWS:
            validate_image(profile["pvc"])
            self.backend = KubeVirtWindowsEnvironment(*args, **kwargs)
            self.backend.settings = replace(self.backend.settings, image=profile["pvc"])
            self.backend.control.settings = self.backend.settings
            if self.backend.settings.guest_protocol != "ale":
                raise ValueError("ALE Windows requires the ALE CUA command protocol")

    @staticmethod
    def type():
        return "ale-cpu"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(windows=True)

    @classmethod
    def resource_capabilities(cls):
        # Resource enforcement is validated by the selected backend.
        return None

    def _validate_resource_mode_support(self):
        # Delegate validates Docker/Windows limits or SBX's fixed template spec.
        pass

    def _validate_definition(self):
        super()._validate_definition()
        if self.task_env_config.gpus:
            raise ValueError("GPU ALE tasks are excluded")
        if self.task_env_config.network_mode.value != "public":
            raise ValueError("ALE does not enforce restricted network policies")
        if self.task_env_config.storage_mb is not None or (self.os == TaskOS.WINDOWS and self.task_env_config.docker_image):
            raise ValueError("ALE uses mapped images; Windows Docker images and disk resizing are unsupported")
        logs = {"/logs/agent", "/logs/verifier", "/logs/artifacts"} if self.os == TaskOS.LINUX else {
            "c:/logs/agent", "c:/logs/verifier", "c:/logs/artifacts"}
        if any(mount["target"].lower() not in logs or mount.get("read_only") for mount in self._mounts):
            raise ValueError("ALE does not support arbitrary host mounts")

    async def start(self, force_build=False):
        if self._started:
            return
        if self._retained:
            raise RuntimeError("A retained ALE guest cannot be reused; create a new environment instance")
        self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.os == TaskOS.WINDOWS:
                await self.backend.start(force_build=force_build)
                self.spec.update(vm=self.backend.vm_name, endpoint=str(self.backend._guest().client.base_url).rstrip("/"))
            else:
                mode = self.linux_backend
                if mode == "auto":
                    mode = "sbx" if sbx_configured(self.spec["profile"]) else "docker"
                try:
                    mode, self.backend = create_backend(self, self.spec["profile"], mode, self.backend_kwargs)
                    await self.backend.start(force_build=force_build)
                except Exception:
                    if mode != "sbx" or self.linux_backend != "auto" or force_build:
                        raise
                    if self.backend:
                        await cleanup_sbx_attempt(self.backend)
                    self.logger.warning("SBX provisioning unavailable; using Docker for this ALE trial")
                    mode, self.backend = create_backend(self, self.spec["profile"], "docker", self.backend_kwargs)
                    await self.backend.start(force_build=False)
                self.proxy = CUAProxy(self.backend, self.spec["profile"])
                endpoint = await self.proxy.start()
                self.spec.update(vm=self.session_id, endpoint=endpoint)
                ownership = {"backend": mode, "session_id": self.session_id,
                    "image": self.spec["profile"].get("docker_image", DEFAULT_IMAGE), "snapshot": self.native["snapshot"]}
                sandbox_id = getattr(getattr(self.backend, "_sandbox", None), "sandbox_id", None)
                if mode == "sbx" and isinstance(sandbox_id, str):
                    ownership["sandbox_id"] = sandbox_id
                (self.trial_paths.trial_dir / "ale-linux.json").write_text(json.dumps(ownership) + "\n")
            await self.native_phase("setup")
            self._started = True
        except BaseException:
            await asyncio.shield(self.stop(delete=True))
            raise

    def _worker_env(self, extra=None):
        return {**os.environ, **(extra or {}), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]) + ":" + self.source}

    async def native_phase(self, phase, verifier_env=None):
        async with self._phase_lock:
            spec = self.trial_paths.trial_dir / "ale-worker.json"
            spec.write_text(json.dumps(self.spec, indent=2) + "\n")
            result = self.trial_paths.trial_dir / f"ale-{phase}.json"
            result.unlink(missing_ok=True)
            with (self.trial_paths.trial_dir / f"ale-{phase}.log").open("wb") as log:
                try:
                    self.worker = await asyncio.create_subprocess_exec(
                        self.native_python, "-m", "ale_adapter.native", phase, str(spec), str(result),
                        env=self._worker_env(verifier_env), stdout=log, stderr=log, start_new_session=True)
                    if await self.worker.wait():
                        raise RuntimeError(f"ALE {phase} failed; see ale-{phase}.log")
                    return json.loads(result.read_text())
                finally:
                    await asyncio.shield(self._stop_worker())

    async def _stop_worker(self):
        worker, self.worker = self.worker, None
        try:
            if worker is not None:
                if worker.returncode is None:
                    if worker.stdin:
                        worker.stdin.close()
                    # Stop native setup/grading before deleting its sandbox.
                    try:
                        os.killpg(worker.pid, signal.SIGINT)
                        await asyncio.wait_for(worker.wait(), timeout=30)
                    except (ProcessLookupError, TimeoutError):
                        if worker.returncode is None:
                            os.killpg(worker.pid, signal.SIGKILL)
                await worker.wait()
        finally:
            self.worker = None

    async def stop(self, delete=True):
        try:
            await self._stop_worker()
            if self.proxy:
                await self.proxy.stop()
        finally:
            if self.backend:
                await self.backend.stop(delete=delete)
            self._retained = not delete
            self._started = False

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        result = await self.backend.exec(command, cwd=cwd, env=self._merge_env(env),
                                         timeout_sec=timeout_sec, user=self._resolve_user(user))
        callback = self._output_callback()
        if callback:
            for stream in ("stdout", "stderr"):
                if getattr(result, stream):
                    await callback(getattr(result, stream), stream)
        return result

    async def upload_file(self, source_path, target_path):
        await self.backend.upload_file(source_path, target_path)

    async def upload_dir(self, source_dir, target_dir):
        await self.backend.upload_dir(source_dir, target_dir)

    async def download_file(self, source_path, target_path):
        await self.backend.download_file(source_path, target_path)

    async def download_dir(self, source_dir, target_dir):
        await self.backend.download_dir(source_dir, target_dir)

    async def download_dir_with_exclusions(self, *, source_dir, target_dir, exclude):
        await self.backend.download_dir_with_exclusions(source_dir=source_dir, target_dir=target_dir, exclude=exclude)

    async def download_dir_filtered(self, *, source_dir, target_dir, include=None, exclude=None, protect=None):
        await self.backend.download_dir_filtered(source_dir=source_dir, target_dir=target_dir,
                                                include=include, exclude=exclude, protect=protect)
