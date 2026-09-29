"""KubeVirt-native VM lifecycle over the Kubernetes API server.

A KubeVirt Windows trial is a `VirtualMachine` (kubevirt.io/v1) that boots
the reused Windows golden image attached as a `hostDisk` (SATA bus, since the
image carries no virtio storage driver), uses the pod network via masquerade
with the WAA port declared, and is reachable from the runner through a NodePort
Service. The control plane talks only to the kube-apiserver over HTTPS using
mTLS credentials loaded from a kubeconfig; it never shells out to `kubectl`.
"""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

OWNER_LABEL = "agent-fleet/trial"

DEFAULT_CPU_CORES = 2
DEFAULT_CPU_SOCKETS = 1
DEFAULT_MEMORY_GUEST = "4Gi"
DEFAULT_DISK_SIZE = "32Gi"
DEFAULT_DISK_BUS = "sata"  # Windows golden images lack a virtio storage driver

_KUBEVIRT_VM_GROUP = "kubevirt.io/v1"
_SUBRESOURCE_GROUP = "apis/subresources.kubevirt.io/v1"

RFC1123_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
NS_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_WINDOWS_SPEC_BASE = {
    "architecture": "amd64",
    "domain": {
        "clock": {"timer": {"hpet": {"present": False}}, "utc": {}},
        "devices": {
            "autoattachPodInterface": True,
            "interfaces": [{
                "masquerade": {}, "model": "virtio", "name": "default",
                "ports": [],  # WAA port added per create
            }],
            "tpm": {},
        },
        "features": {"acpi": {"enabled": True}, "smm": {"enabled": True}},
        "firmware": {"bootloader": {"efi": {"secureBoot": True}}},
        "machine": {"type": "pc-q35-rhel9.2.0"},
    },
    "networks": [{"name": "default", "pod": {}}],
}

_CLOUD_INIT = "#cloud-config\n# Windows WAA image; guest server starts at logon.\n"


class KubeVirtError(RuntimeError):
    """Raised when the Kubernetes/KubeVirt API rejects a request."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


PlatformAPIError = KubeVirtError  # compatibility alias


@dataclass(frozen=True)
class Cluster:
    api_server: str
    ca_data: str
    client_cert_data: str
    client_key_data: str


@dataclass(frozen=True)
class Settings:
    cluster: Cluster
    image: str
    namespace: str
    node: str
    disk_bus: str = DEFAULT_DISK_BUS
    waa_port: int = 5000
    waa_node_port: int = 0
    start_timeout: int = 1800
    command_timeout: int = 3600
    transfer_timeout: int = 300

    @classmethod
    def from_env(cls):
        kubeconfig = os.environ.get(
            "HARBOR_KUBEVIRT_KUBECONFIG", os.path.expanduser("~/.kube/config")
        )
        api_server = os.environ.get("HARBOR_KUBEVIRT_API_SERVER")
        image = os.environ.get("HARBOR_KUBEVIRT_IMAGE", "")
        namespace = os.environ.get("HARBOR_KUBEVIRT_NAMESPACE", "")
        if not namespace:
            namespace = "default"
        if not NS_RE.fullmatch(namespace):
            raise ValueError("Invalid Kubernetes namespace")
        if not image:
            raise ValueError("HARBOR_KUBEVIRT_IMAGE (host disk path) is required")
        if api_server and not URL_RE.match(api_server):
            raise ValueError("HARBOR_KUBEVIRT_API_SERVER must use http:// or https://")
        node = os.environ.get("HARBOR_KUBEVIRT_NODE", "")
        kc_server, ca, cert, key = _load_kubeconfig(kubeconfig)
        if not api_server:
            api_server = kc_server

        def as_int(key, default):
            value = os.environ.get("HARBOR_KUBEVIRT_" + key)
            try:
                return int(value) if value is not None and value != "" else default
            except ValueError:
                raise ValueError(
                    f"HARBOR_KUBEVIRT_{key} must be an integer, got {value!r}"
                ) from None

        waa_port = as_int("WAA_PORT", 5000)
        waa_node_port = as_int("WAA_NODE_PORT", 0)
        start_timeout = as_int("START_TIMEOUT", 1800)
        command_timeout = as_int("COMMAND_TIMEOUT", 3600)
        transfer_timeout = as_int("TRANSFER_TIMEOUT", 300)
        if min(start_timeout, command_timeout, transfer_timeout) <= 0:
            raise ValueError("Timeouts must be positive")
        if not 1 <= waa_port <= 65535:
            raise ValueError("WAA port must be between 1 and 65535")
        disk_bus = os.environ.get("HARBOR_KUBEVIRT_DISK_BUS", DEFAULT_DISK_BUS)
        if disk_bus not in ("sata", "virtio", "scsi"):
            raise ValueError("disk_bus must be one of: sata, virtio, scsi")
        return cls(
            cluster=Cluster(
                api_server=api_server,
                ca_data=ca,
                client_cert_data=cert,
                client_key_data=key,
            ),
            image=image,
            namespace=namespace,
            node=node,
            disk_bus=disk_bus,
            waa_port=waa_port,
            waa_node_port=waa_node_port,
            start_timeout=start_timeout,
            command_timeout=command_timeout,
            transfer_timeout=transfer_timeout,
        )


def _load_kubeconfig(path: str):
    """Return (api_server, ca_data, client_cert_data, client_key_data).

    Reads the current cluster/user from a kubeconfig. No `kubectl` is invoked;
    traffic goes straight to the apiserver over mTLS.
    """
    kpath = Path(os.path.expanduser(path))
    if not kpath.exists():
        raise ValueError(f"HARBOR_KUBEVIRT_KUBECONFIG not found: {kpath}")
    with kpath.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    contexts = {c.get("name"): c.get("context") or {} for c in cfg.get("contexts", [])}
    current = cfg.get("current-context")
    if not current or current not in contexts:
        raise ValueError("No current-context in kubeconfig")
    ctx = contexts[current]
    cl_name = ctx.get("cluster")
    us_name = ctx.get("user")
    cluster = next(
        (c.get("cluster") or {} for c in cfg.get("clusters", [])
         if c.get("name") == cl_name), {}
    )
    user = next(
        (u.get("user") or {} for u in cfg.get("users", [])
         if u.get("name") == us_name), {}
    )
    api_server = cluster.get("server")
    if not api_server:
        raise ValueError("kubeconfig cluster has no server")
    ca = _decode(cluster.get("certificate-authority-data")) or (
        _read_b64_file(cluster, "certificate-authority") if cluster.get("certificate-authority") else None
    )
    cert = _decode(user.get("client-certificate-data")) or (
        _read_b64_file(user, "client-certificate") if user.get("client-certificate") else None
    )
    key = _decode(user.get("client-key-data")) or (
        _read_b64_file(user, "client-key") if user.get("client-key") else None
    )
    if not all((api_server, cert, key)):
        raise ValueError(
            "kubeconfig cluster/user must provide mTLS client cert/key (or token auth is unsupported)"
        )
    return api_server, ca or "", cert, key


def _decode(data):
    if not data:
        return None
    try:
        return base64.b64decode(data).decode("utf-8")
    except (ValueError, TypeError):
        return data


def _read_b64_file(container, key):
    path = container.get(key)
    if not path:
        return None
    try:
        return Path(os.path.expanduser(path)).read_bytes().decode("utf-8")
    except OSError:
        return None


def pick_root_disk_size(configured: str, min_size: str | None) -> str:
    """A hostDisk clone has no CDI minSize; return the configured default."""
    return configured


def build_create_request(
    settings: Settings,
    name: str,
    labels: dict | None = None,
    *,
    cpus: int | None = None,
    memory_mb: int | None = None,
    disk_bus: str | None = None,
) -> dict:
    """Build a `VirtualMachine` (kubevirt.io/v1) CR reusing the golden image.

    The root disk is a `hostDisk` on `settings.node` pointing at
    `settings.image` (the existing same-host raw image, e.g. minimal.raw).
    The pod-network masquerade interface declares the WAA port; a NodePort
    Service (created by the control plane) exposes it to the runner.
    """
    disk_bus = disk_bus or settings.disk_bus
    if not name or not RFC1123_NAME_RE.fullmatch(name):
        raise ValueError("VM name must be a lowercase RFC1123 DNS subdomain")
    if disk_bus not in ("sata", "virtio", "scsi"):
        raise ValueError("disk_bus must be one of: sata, virtio, scsi")
    for value in (cpus, memory_mb):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("CPU and memory sizes must be positive integers")
    cores = cpus if cpus is not None else DEFAULT_CPU_CORES
    memory = f"{memory_mb}Mi" if memory_mb is not None else DEFAULT_MEMORY_GUEST

    spec = jsonable(_WINDOWS_SPEC_BASE)
    spec["domain"]["cpu"] = {
        "cores": cores, "sockets": DEFAULT_CPU_SOCKETS, "threads": 1,
    }
    spec["domain"]["devices"]["interfaces"][0]["ports"] = [
        {"name": "waa", "port": settings.waa_port, "protocol": "TCP"}
    ]
    spec["domain"]["resources"] = {
        "limits": {"memory": memory},
        "requests": {"memory": memory},
    }
    spec["volumes"] = [
        {
            "name": "disk0",
            "hostDisk": {
                "path": settings.image,
                "type": "Disk",
                "capacity": "0",
            },
        },
        {
            "name": "cloudinit",
            "cloudInitNoCloud": {
                "userDataBase64": base64.b64encode(_CLOUD_INIT.encode("utf-8")).decode("ascii")
            },
        },
    ]
    root_disk = {"disk": {"bus": disk_bus}, "name": "disk0"}
    cloudinit_disk = {"disk": {"bus": "virtio"}, "name": "cloudinit"}
    spec["domain"]["devices"]["disks"] = [root_disk, cloudinit_disk]
    if settings.node:
        spec["nodeSelector"] = {"kubernetes.io/hostname": settings.node}

    vm_labels = dict(labels or {})
    vm_labels.setdefault("kubevirt.io/domain", name)
    return {
        "apiVersion": _KUBEVIRT_VM_GROUP,
        "kind": "VirtualMachine",
        "metadata": {
            "name": name,
            "namespace": settings.namespace,
            "labels": vm_labels,
        },
        "spec": {
            "running": False,
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": name}},
                "spec": spec,
            },
        },
    }


def jsonable(value):
    return __import__("copy").deepcopy(value)


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class KubeVirtControl:
    """Async apiserver client for KubeVirt VirtualMachine lifecycle.

    Uses mTLS credentials from the kubeconfig; no kubectl, virtctl, or client
    library. Created per environment instance; never performs I/O at import.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._client = None
        self._tmp = None
        self._cert_path = None
        self._verify_path = None

    def _materialize(self):
        if self._verify_path is not None:
            return
        self._tmp = tempfile.TemporaryDirectory(prefix="kubevirt-ctl-")
        tmp = Path(self._tmp.name)
        self._verify_path = str(tmp / "ca.pem")
        cert = tmp / "client.crt"
        key = tmp / "client.key"
        cert.write_text(self.settings.cluster.client_cert_data, encoding="utf-8")
        key.write_text(self.settings.cluster.client_key_data, encoding="utf-8")
        with open(self._verify_path, "w", encoding="utf-8") as fh:
            fh.write(self.settings.cluster.ca_data)
        self._cert_path = (str(cert), str(key))

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._materialize()
            ssl_context = ssl.create_default_context(cafile=self._verify_path)
            ssl_context.load_cert_chain(*self._cert_path)
            self._client = httpx.AsyncClient(
                base_url=self.settings.cluster.api_server,
                verify=ssl_context,
                timeout=httpx.Timeout(30.0),
                transport=self._transport,
            )
        return self._client

    def _vm_path(self, name=None):
        ns = self.settings.namespace
        return f"/apis/{_KUBEVIRT_VM_GROUP}/namespaces/{ns}/virtualmachines/{name}"

    def _vmi_path(self, name):
        ns = self.settings.namespace
        return f"/apis/{_KUBEVIRT_VM_GROUP}/namespaces/{ns}/virtualmachineinstances/{name}"

    def _sub_path(self, name, action):
        return (
            f"/{_SUBRESOURCE_GROUP}/namespaces/{self.settings.namespace}/"
            f"virtualmachines/{name}/{action}"
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self._cert_path = None
            self._verify_path = None

    async def ping(self) -> bool:
        try:
            response = await self.client.get("/version")
            response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def create(
        self,
        name: str,
        labels: dict | None = None,
        *,
        cpus: int | None = None,
        memory_mb: int | None = None,
        disk_bus: str | None = None,
    ) -> dict:
        body = build_create_request(
            self.settings, name, labels, cpus=cpus, memory_mb=memory_mb,
            disk_bus=disk_bus,
        )
        response = await self.client.post(
            f"/apis/{_KUBEVIRT_VM_GROUP}/namespaces/{self.settings.namespace}/virtualmachines",
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            msg = response.text[:500]
            raise KubeVirtError(
                f"KubeVirt create failed ({response.status_code}): {msg}",
                code=response.status_code,
            ) from None
        return response.json().get("metadata", {})

    async def get(self, name: str) -> dict:
        response = await self.client.get(self._vm_path(name))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise exc from None
        vm = response.json() or {}
        metadata = vm.get("metadata", {})
        spec = vm.get("spec", {})
        status = vm.get("status", {}) or {}
        labels = metadata.get("labels") or spec.get("template", {}).get("metadata", {}).get("labels") or {}
        vm_status = status.get("printableStatus") or status.get("phase") or ""

        # The pod-network IP lives on the VMI. The VM defines the desired state;
        # a running target has a VMI. Absence of the VMI means not started.
        ip = ""
        phase = vm_status
        vmi_running = False
        try:
            vmi_resp = await self.client.get(self._vmi_path(name))
            vmi_resp.raise_for_status()
            vmi = vmi_resp.json() or {}
            vmi_status = vmi.get("status", {}) or {}
            phase = vmi_status.get("phase", vm_status)
            vmi_running = phase.lower() == "running"
            interfaces = vmi_status.get("interfaces") or []
            for item in interfaces:
                if item.get("ipAddress"):
                    ip = item["ipAddress"]
                    break
        except httpx.HTTPStatusError as exc:
            # No VMI yet (VM not running) is expected when queried pre-start.
            if exc.response.status_code != 404:
                raise
        return {
            "name": name,
            "namespace": self.settings.namespace,
            "ip": ip,
            "ready": bool(ip) and vmi_running,
            "labels": labels,
            "status": phase,
            "uid": metadata.get("uid", ""),
            "running": _as_bool(status.get("ready")),
        }

    async def start(self, name: str) -> None:
        response = await self.client.put(self._sub_path(name, "start"))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise KubeVirtError(
                f"KubeVirt start failed ({response.status_code}): {response.text[:300]}",
                code=response.status_code,
            ) from None

    async def stop(self, name: str) -> None:
        response = await self.client.put(self._sub_path(name, "stop"))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise KubeVirtError(
                f"KubeVirt stop failed ({response.status_code}): {response.text[:300]}",
                code=response.status_code,
            ) from None

    async def delete(self, name: str) -> None:
        response = await self.client.delete(self._vm_path(name))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        await self._delete_service(name)

    async def expose_waa(self, name: str) -> str:
        """Create a NodePort Service exposing WAA and return ``host:port``."""
        service_name = f"{name}-waa"
        svc = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": self.settings.namespace,
                "labels": {OWNER_LABEL: name},
            },
            "spec": {
                "type": "NodePort",
                "selector": {"kubevirt.io/domain": name},
                "ports": [{
                    "name": "waa",
                    "port": self.settings.waa_port,
                    "targetPort": self.settings.waa_port,
                    "protocol": "TCP",
                }],
            },
        }
        if self.settings.waa_node_port:
            svc["spec"]["ports"][0]["nodePort"] = self.settings.waa_node_port
        create = await self.client.post(
            f"/api/v1/namespaces/{self.settings.namespace}/services",
            json=svc,
        )
        if create.status_code != 201 and create.status_code != 200:
            # Retry idempotently on a stale/existing service.
            existing = await self.client.get(
                f"/api/v1/namespaces/{self.settings.namespace}/services/{service_name}"
            )
            if existing.status_code != 200:
                raise KubeVirtError(
                    f"WAA Service create failed ({create.status_code}): {create.text[:200]}",
                    code=create.status_code,
                )
        get_svc = await self.client.get(
            f"/api/v1/namespaces/{self.settings.namespace}/services/{service_name}"
        )
        get_svc.raise_for_status()
        spec = get_svc.json().get("spec", {})
        node_port = spec.get("ports", [{}])[0].get("nodePort")
        if not node_port:
            raise KubeVirtError("WAA Service returned no nodePort")
        host = await self._waa_host(name)
        return f"{host}:{node_port}"

    async def _waa_host(self, name: str) -> str:
        """Return the InternalIP of the node running this VM's VMI."""
        try:
            vmi_resp = await self.client.get(self._vmi_path(name))
            vmi_resp.raise_for_status()
            node_name = (vmi_resp.json().get("status") or {}).get("nodeName")
        except (httpx.HTTPStatusError, AttributeError):
            node_name = None
        if not node_name:
            node_name = self.settings.node
        if node_name:
            node_resp = await self.client.get(f"/api/v1/nodes/{node_name}")
            if node_resp.status_code == 200:
                for addr in (node_resp.json().get("status") or {}).get("addresses", []):
                    if addr.get("type") == "InternalIP":
                        return addr["address"]
        raise KubeVirtError(f"Could not resolve a reachable host for VM {name}")

    async def _delete_service(self, name: str) -> None:
        service_name = f"{name}-waa"
        response = await self.client.delete(
            f"/api/v1/namespaces/{self.settings.namespace}/services/{service_name}"
        )
        if response.status_code not in (200, 404, 202):
            raise KubeVirtError(
                f"WAA Service delete failed ({response.status_code}): {response.text[:200]}",
                code=response.status_code,
            )

    async def available_ips(self) -> list:
        """KubeVirt allocates pod IPs automatically; no pre-allocator exists."""
        return []

    async def image_min_size(self, image: str | None = None) -> str | None:
        """hostDisk clones have no CDI minSize; sizing is irrelevant."""
        return None


def raise_for_platform(response: httpx.Response) -> None:
    """Compatibility shim: raise on non-2xx HTTP status."""
    response.raise_for_status()