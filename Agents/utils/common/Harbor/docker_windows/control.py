"""Own a Dockur container and an independent storage copy for each trial."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

OWNER_LABEL = "agent-fleet.windows-owner"
DEFAULT_IMAGE = "dockurr/windows@sha256:0cff9eb0e7aee9953e55bc682852ca4fdca233145a58ae1ec94f0b0c01a2ed30"
GUEST_PORTS = (5000, 9222, 8080)


@dataclass(frozen=True)
class Settings:
    image: str
    storage: Path
    instances: Path
    start_timeout: int = 1800
    command_timeout: int = 3600
    transfer_timeout: int = 300
    boot_env: tuple[tuple[str, str], ...] = ()
    guest_protocol: str = "waa"
    guest_port: int = 5000
    waa_port: int = 5000

    @classmethod
    def from_env(cls):
        source = os.environ.get("HARBOR_WAA_DOCKER_STORAGE", "")
        if not source:
            raise ValueError("HARBOR_WAA_DOCKER_STORAGE must name a shut-down, prepared Dockur storage directory")
        storage = Path(source).expanduser().resolve()
        if not storage.is_dir() or not any((storage / name).is_file() for name in ("data.img", "data.qcow2")):
            raise ValueError("Dockur storage requires an existing data.img or self-contained data.qcow2")
        for name in ("data.img", "data.qcow2"):
            disk = storage / name
            if disk.is_file():
                with disk.open("rb") as stream:
                    header = stream.read(20)
                if header[:4] == b"QFI\xfb" and (len(header) < 20 or any(struct.unpack(">QI", header[8:20]))):
                    raise ValueError("Golden QCOW2 disks must be flattened; backing files are unsupported")
        # A copied symlink can still point at the golden image's mutable files.
        if any(path.is_symlink() for path in storage.rglob("*")):
            raise ValueError("Golden Dockur storage must not contain symlinks")
        cache = Path(os.environ.get("AGENT_FLEET_CACHE_DIR", "~/.cache/agent-fleet")).expanduser()
        instances = Path(os.environ.get("HARBOR_WAA_DOCKER_INSTANCES", str(cache / "waa/docker"))).expanduser().resolve()
        if instances.is_relative_to(storage) or storage.is_relative_to(instances):
            raise ValueError("Dockur golden storage and trial directories must be separate")
        if any("," in str(path) for path in (storage, instances)):
            raise ValueError("Dockur bind mount paths must not contain commas")
        image = os.environ.get("HARBOR_WAA_DOCKER_IMAGE", DEFAULT_IMAGE)
        if not image or image.startswith("-") or any(c.isspace() for c in image):
            raise ValueError("HARBOR_WAA_DOCKER_IMAGE must be a Docker image reference")
        timeouts = {key: int(os.environ.get("HARBOR_WAA_DOCKER_" + key.upper(), default))
                    for key, default in (("start_timeout", 1800), ("command_timeout", 3600), ("transfer_timeout", 300))}
        if any(value <= 0 for value in timeouts.values()):
            raise ValueError("Dockur timeouts must be positive")
        boot_env = {key: os.environ["HARBOR_WAA_DOCKER_" + key]
                    for key in ("VERSION", "BOOT_MODE", "DISK_TYPE", "DISK_FMT", "DISK_SIZE")
                    if "HARBOR_WAA_DOCKER_" + key in os.environ}
        boot_env.setdefault("DISK_FMT", "qcow2" if (storage / "data.qcow2").exists() else "raw")
        return cls(image=image, storage=storage, instances=instances,
                   boot_env=tuple(boot_env.items()), **timeouts)


async def run(*argv, timeout=120, missing_ok=False):
    process = await asyncio.create_subprocess_exec(
        *map(str, argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        if missing_ok and b"No such container" in stderr:
            return None
        # Docker output can contain private configuration. Keep it in Docker's
        # logs rather than exposing it through Harbor errors or dry runs.
        raise RuntimeError(f"{argv[0]} {argv[1]} failed (exit {process.returncode})")
    return stdout.decode()


class DockerControl:
    def __init__(self, settings):
        self.settings = settings

    async def preflight(self):
        context = os.environ.get("DOCKER_CONTEXT")
        endpoint = None if context else os.environ.get("DOCKER_HOST")
        if not endpoint:
            data = json.loads(await run("docker", "context", "inspect", *([context] if context else [])))
            endpoint = data[0]["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("unix://"):
            raise ValueError("Dockur requires a local Docker daemon and local storage paths")
        info = json.loads(await run("docker", "info", "--format", "{{json .}}"))
        if info.get("OSType") != "linux" or any("rootless" in item for item in info.get("SecurityOptions", [])):
            raise ValueError("Dockur requires rootful Linux Docker with KVM access")
        for device in ("/dev/kvm", "/dev/net/tun"):
            if not Path(device).exists():
                raise ValueError(f"Dockur requires {device}")
        await run("docker", "image", "inspect", self.settings.image)
        # Do not copy a running VM's storage. Check all running local containers,
        # including manually created instances with no Fleet labels.
        names = (await run("docker", "ps", "--quiet")).split()
        if names:
            containers = json.loads(await run("docker", "inspect", *names))
            for container in containers:
                for mount in container.get("Mounts", []):
                    if mount.get("Type") == "bind" and mount.get("RW"):
                        source = Path(mount["Source"]).resolve()
                        if source.is_relative_to(self.settings.storage) or self.settings.storage.is_relative_to(source):
                            raise ValueError("Golden Dockur storage is mounted writable by a running container")

    def instance(self, name):
        if not name.startswith("hf-dockur-") or not name.removeprefix("hf-dockur-").isalnum():
            raise ValueError("Invalid Dockur trial name")
        return self.settings.instances / name

    async def inspect(self, name, *, owner):
        output = await run("docker", "container", "inspect", name, missing_ok=True)
        if output is None:
            return None
        container = json.loads(output)[0]
        if container["Config"].get("Labels", {}).get(OWNER_LABEL) != owner:
            raise RuntimeError(f"Dockur container ownership mismatch: {name}")
        return container

    async def create(self, name, *, owner, cpus, memory_mb):
        root = self.instance(name)
        root.mkdir(parents=True, exist_ok=False)
        (root / "owner").write_text(owner)
        storage = root / "storage"
        storage.mkdir()
        (root / "shared").mkdir()
        # Never hardlink guest disks. Reflinks copy on write when supported;
        # otherwise GNU cp preserves sparse files in a full independent copy.
        await run("cp", "-a", "--reflink=auto", "--sparse=always", "--no-preserve=ownership", "--",
                  str(self.settings.storage) + "/.", storage, timeout=self.settings.start_timeout)
        args = ["docker", "create", "--name", name, "--platform", "linux/amd64",
                "--label", f"{OWNER_LABEL}={owner}", "--restart", "no", "--stop-timeout", "120",
                "--device", "/dev/kvm", "--device", "/dev/net/tun", "--cap-add", "NET_ADMIN",
                "--env", f"CPU_CORES={cpus or 4}", "--env", f"RAM_SIZE={memory_mb or 8192}M",
                "--mount", f"type=bind,src={storage},dst=/storage",
                "--mount", f"type=bind,src={root / 'shared'},dst=/shared"]
        for port in GUEST_PORTS:
            args += ["--publish", f"127.0.0.1::{port}/tcp"]
        for key, value in self.settings.boot_env:
            args += ["--env", f"{key}={value}"]
        await run(*args, self.settings.image)

    async def start(self, name):
        await run("docker", "start", name)

    async def guest_endpoints(self, name, *, owner):
        container = await self.inspect(name, owner=owner)
        if container is None or not container["State"]["Running"]:
            raise RuntimeError("Dockur container exited before guest readiness; see docker.log")
        ports = container["NetworkSettings"]["Ports"]
        return {port: "http://127.0.0.1:" + ports[f"{port}/tcp"][0]["HostPort"] for port in GUEST_PORTS}

    async def delete(self, name, *, owner, delete=True):
        container = await self.inspect(name, owner=owner)
        if container is not None:
            if delete:
                # Removal also releases the random published ports. No --volumes:
                # all mutable data lives in the explicitly owned trial directory.
                await run("docker", "rm", "--force", name)
            elif container["State"]["Running"]:
                await run("docker", "stop", "--timeout", "120", name, timeout=150)
        root = self.instance(name)
        if delete and root.exists():
            if root.is_symlink() or (root / "owner").read_text() != owner:
                raise RuntimeError("Dockur storage ownership mismatch")
            try:
                # Keep the ownership marker until all mutable data is gone, so
                # a failed removal remains safe to retry.
                for child in ("storage", "shared"):
                    if (root / child).exists():
                        await asyncio.to_thread(shutil.rmtree, root / child)
            except PermissionError:
                # Dockur can leave root-owned TPM directories. Only the already
                # checked trial tree is mounted into this short-lived helper.
                helper = name + "-cleanup"
                try:
                    await run("docker", "run", "--name", helper, "--rm", "--network", "none",
                              "--label", f"{OWNER_LABEL}={owner}", "--entrypoint", "/bin/sh",
                              "--mount", f"type=bind,src={root},dst=/trial",
                              self.settings.image, "-c", "rm -rf /trial/storage /trial/shared")
                finally:
                    if await self.inspect(helper, owner=owner) is not None:
                        await run("docker", "rm", "--force", helper)
            (root / "owner").unlink()
            root.rmdir()

    async def logs(self, name, target):
        # Stream to disk; boot logs need not fit in the controller's memory.
        output = await asyncio.to_thread(Path(target).open, "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "logs", name, stdout=output, stderr=output,
            )
            try:
                async with asyncio.timeout(30):
                    await process.wait()
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
        finally:
            output.close()
