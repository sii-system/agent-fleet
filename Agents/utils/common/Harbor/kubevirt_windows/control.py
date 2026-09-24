"""Platform-HTTP VM lifecycle; no credentials are copied into the guest."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

OWNER_LABEL = "agent-fleet/trial"

DEFAULT_CPU_CORES = 2
DEFAULT_CPU_SOCKETS = 1
DEFAULT_MEMORY_GUEST = "4Gi"
DEFAULT_DISK_SIZE = "32Gi"  # minimum rootDisk size; the larger of this and the source template's minSize wins


_QUANTITY_UNITS = {
    "Ki": 1 << 10,
    "Mi": 1 << 20,
    "Gi": 1 << 30,
    "Ti": 1 << 40,
    "Pi": 1 << 50,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
}


def _quantity_bytes(value) -> int | None:
    """Parse a Kubernetes quantity string (e.g. "40Gi", "5G") into bytes.

    Returns None for unparseable values so callers can fall back safely.
    Only the suffixes the platform emits for disk sizes (Ki/Mi/Gi/Ti, K/M/G/T)
    are handled; bare integers are treated as bytes.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    for suffix, factor in _QUANTITY_UNITS.items():
        if value.endswith(suffix):
            number = value[: -len(suffix)].strip()
            try:
                return int(float(number) * factor)
            except ValueError:
                return None
    try:
        return int(value)
    except ValueError:
        return None


def pick_root_disk_size(configured: str, min_size: str | None) -> str:
    """Choose the root disk size for a create request.

    A clone of a template image must be at least as large as the source
    image's minSize, otherwise CDI provisioning hangs/fails. Returns the
    larger of the configured default and the template minSize (when known).
    """
    configured_bytes = _quantity_bytes(configured)
    min_bytes = _quantity_bytes(min_size) if min_size else None
    if configured_bytes is None and min_bytes is None:
        return configured
    if min_bytes is None or (configured_bytes is not None and configured_bytes >= min_bytes):
        return configured
    return min_size or configured

# Lowercase RFC 1123 DNS subdomain (max 63): what the platform requires for VM
# names because it auto-creates a guest-credential Secret named after the VM.
RFC1123_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class PlatformAPIError(RuntimeError):
    """Raised when the platform returns a non-2xx HTTP status or envelope code."""


async def run_process(
    argv, *, data=None, timeout=60, max_output_bytes=16 * 1024 * 1024
):
    """Bound subprocess output/lifetime, including ProxyCommand descendants."""
    process = await asyncio.create_subprocess_exec(
        *map(str, argv),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read_bounded(stream):
        output = bytearray()
        while chunk := await stream.read(65536):
            if len(output) + len(chunk) > max_output_bytes:
                raise RuntimeError(f"{Path(argv[0]).name} exceeded its output limit")
            output.extend(chunk)
        return bytes(output)

    async def write_input():
        try:
            if data:
                process.stdin.write(data)
                await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Report the subprocess exit status after collecting stderr.

    tasks = [
        asyncio.create_task(coro)
        for coro in (
            read_bounded(process.stdout),
            read_bounded(process.stderr),
            write_input(),
            process.wait(),
        )
    ]
    try:
        stdout, stderr, _, _ = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await process.wait()
        raise
    if process.returncode:
        # Do not print argv or stdin: requests can contain guest credentials.
        raise RuntimeError(
            f"{Path(argv[0]).name} failed ({process.returncode}): "
            f"{stderr.decode(errors='replace')[-4000:]}"
        )
    return stdout


URL_RE = re.compile(r"^https?://", re.IGNORECASE)
NS_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class Platform:
    base_url: str
    token: str


@dataclass(frozen=True)
class Settings:
    platform: Platform
    image: str
    namespace: str
    ssh_user: str
    ssh_key: Path
    subnet: str
    storage_class: str
    ssh_port: int = 22
    start_timeout: int = 1800
    command_timeout: int = 3600
    transfer_timeout: int = 300

    @classmethod
    def from_env(cls):
        def required(key):
            value = os.environ.get("HARBOR_KUBEVIRT_" + key, "")
            if not value:
                raise ValueError(f"HARBOR_KUBEVIRT_{key} is required")
            return value

        base_url = required("BASE_URL")
        if not URL_RE.match(base_url):
            raise ValueError("HARBOR_KUBEVIRT_BASE_URL must use http:// or https://")
        namespace = required("NAMESPACE")
        if not NS_RE.fullmatch(namespace):
            raise ValueError("Invalid Kubernetes namespace")
        ssh_user = required("SSH_USER")
        if not ssh_user or any(c in ssh_user for c in "\r\n\x00@"):
            raise ValueError("Invalid SSH user")
        ssh_key = Path(required("SSH_KEY")).expanduser()
        if not ssh_key.is_file():
            raise FileNotFoundError(ssh_key)
        image = required("IMAGE")
        subnet = os.environ.get("HARBOR_KUBEVIRT_SUBNET", "ovn-default")
        storage_class = os.environ.get("HARBOR_KUBEVIRT_STORAGE_CLASS", "ceph-rbd-sc")

        def as_int(key, default):
            value = os.environ.get("HARBOR_KUBEVIRT_" + key)
            try:
                return int(value) if value is not None and value != "" else default
            except ValueError:
                raise ValueError(
                    f"HARBOR_KUBEVIRT_{key} must be an integer, got {value!r}"
                ) from None

        ssh_port = as_int("SSH_PORT", 22)
        start_timeout = as_int("START_TIMEOUT", 1800)
        command_timeout = as_int("COMMAND_TIMEOUT", 3600)
        transfer_timeout = as_int("TRANSFER_TIMEOUT", 300)
        if min(start_timeout, command_timeout, transfer_timeout) <= 0:
            raise ValueError("Timeouts must be positive")
        if not 1 <= ssh_port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        return cls(
            platform=Platform(base_url=base_url, token=required("TOKEN")),
            image=image,
            namespace=namespace,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            subnet=subnet,
            storage_class=storage_class,
            ssh_port=ssh_port,
            start_timeout=start_timeout,
            command_timeout=command_timeout,
            transfer_timeout=transfer_timeout,
        )


def _api_base(base_url: str) -> str:
    """Normalize a human-supplied base URL into the API v1 endpoint."""
    return base_url.rstrip("/") + "/api/v1"


def raise_for_platform(response: httpx.Response) -> None:
    """Raise if the HTTP status or the envelope's `code` is not 2xx."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise exc from None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return  # No JSON envelope to inspect; the HTTP status already passed.
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if isinstance(code, int) and not 200 <= code < 300:
        raise PlatformAPIError(
            f"Platform API error (code={code}): {payload.get('message', '')}"
        )


def build_create_request(
    settings: Settings, name: str, ip: str, labels: dict | None = None, disk_size: str = DEFAULT_DISK_SIZE
) -> dict:
    """Build the CreateVMRequest envelope accepted by POST /virtualmachines.

    `labels` is an operator metadata map (e.g. {OWNER_LABEL: tag}); the platform
    declares CreateVMRequest.labels as an object of string values. `disk_size`
    is the root disk size as a Kubernetes quantity string (e.g. "40Gi"); callers
    should pass the larger of the default and the source template's minSize.
    """
    if not name or not RFC1123_NAME_RE.fullmatch(name):
        raise ValueError(
            "VM name must be a lowercase RFC1123 DNS subdomain (max 63 chars)"
        )
    if not ip:
        raise ValueError("A subnet IP address is required")
    request: dict[str, Any] = {
        "name": name,
        "namespace": settings.namespace,
        "createType": "template",
        "compute": {
            "cpuCores": DEFAULT_CPU_CORES,
            "cpuSockets": DEFAULT_CPU_SOCKETS,
            "memoryGuest": DEFAULT_MEMORY_GUEST,
        },
        "network": {"subnetName": settings.subnet, "ipAddress": ip},
        "storage": {
            "rootDisk": {
                "imageName": settings.image,
                "size": disk_size,
                "storageClassName": settings.storage_class,
            }
        },
    }
    if labels:
        request["labels"] = labels
    return request


def _as_bool(value) -> bool:
    """Coerce a ready flag that may arrive as bool, int, or string."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def parse_vm(data: dict) -> dict:
    """Normalize a GET VM `data` payload into a stable, tolerant dict."""
    metadata = data.get("metadata") or {}
    labels = data.get("labels") or metadata.get("labels") or {}
    return {
        "name": data.get("name") or metadata.get("name") or "",
        "namespace": data.get("namespace") or metadata.get("namespace") or "",
        "ip": data.get("ipAddress") or "",
        "ready": _as_bool(data.get("ready")),
        "labels": labels,
        "status": data.get("printableStatus")
        or data.get("status")
        or (metadata.get("status") or ""),
        "uid": data.get("uid") or metadata.get("uid") or "",
    }


class PlatformControl:
    """Async client for the platform HTTP VM lifecycle API.

    Replaces the kubectl-based VMControl. Uses direct bearer-token auth; the
    client is created per instance and never performs I/O at import time.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=_api_base(settings.platform.base_url),
            headers={"Authorization": f"Bearer {settings.platform.token}"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def create(
        self, name: str, ip: str, labels: dict | None = None, disk_size: str = DEFAULT_DISK_SIZE
    ) -> dict:
        response = await self._client.post(
            "/virtualmachines",
            json=build_create_request(self.settings, name, ip, labels, disk_size),
        )
        raise_for_platform(response)
        body = response.json()
        return body.get("data", {}) if isinstance(body, dict) else {}

    async def image_min_size(self, image: str | None = None) -> str | None:
        """Return the source image/template's minSize (e.g. "40Gi") or None.

        A clone of a template must be at least this large to provision; the
        caller should size the root disk to at least this value.
        """
        image = image or self.settings.image
        if not image:
            return None
        response = await self._client.get(
            f"/images/{self.settings.namespace}/{image}"
        )
        raise_for_platform(response)
        body = response.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        min_size = data.get("minSize") if isinstance(data, dict) else None
        return min_size if isinstance(min_size, str) and min_size else None

    async def get(self, name: str) -> dict:
        response = await self._client.get(
            f"/virtualmachines/{self.settings.namespace}/{name}"
        )
        raise_for_platform(response)
        body = response.json()
        return parse_vm(body.get("data", {}) if isinstance(body, dict) else {})

    async def available_ips(self, subnet: str | None = None) -> list:
        """Return free subnet IPs the platform may assign to a new VM.

        The platform requires a concrete network.ipAddress at create time and
        direct-IP SSH needs a known host, so a free IP is picked before create.
        GET /network/ips is admin-only (403); the per-subnet available-ips
        listing is the read-only call that works for this role.
        """
        subnet = subnet or self.settings.subnet
        response = await self._client.get(
            f"/network/subnets/{subnet}/available-ips"
        )
        raise_for_platform(response)
        body = response.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        ips = data.get("ips") if isinstance(data, dict) else data
        if not isinstance(ips, list):
            raise PlatformAPIError("available-ips returned unexpected shape")
        return [ip for ip in ips if isinstance(ip, str) and ip]

    async def start(self, name: str) -> dict:
        """Power on an existing VM. Create leaves the VM defined/Stopped and an
        explicit start is required before the guest boots and becomes ready."""
        response = await self._client.put(
            f"/virtualmachines/{self.settings.namespace}/{name}/start"
        )
        raise_for_platform(response)
        body = response.json()
        return body.get("data", {}) if isinstance(body, dict) else {}

    async def stop(self, name: str) -> dict:
        response = await self._client.put(
            f"/virtualmachines/{self.settings.namespace}/{name}/stop"
        )
        raise_for_platform(response)
        body = response.json()
        return body.get("data", {}) if isinstance(body, dict) else {}

    async def delete(self, name: str) -> dict:
        response = await self._client.delete(
            f"/virtualmachines/{self.settings.namespace}/{name}"
        )
        raise_for_platform(response)
        body = response.json()
        return body.get("data", {}) if isinstance(body, dict) else {}

    async def ping(self) -> bool:
        try:
            response = await self._client.get("/users/me")
            raise_for_platform(response)
            return True
        except (httpx.HTTPError, PlatformAPIError):
            return False
