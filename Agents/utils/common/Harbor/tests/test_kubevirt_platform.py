"""Offline unit tests for the KubeVirt-native lifecycle and request builder."""

from __future__ import annotations

import contextlib
import os
import unittest

import httpx
from kubevirt_windows.control import (
    DEFAULT_CPU_CORES,
    DEFAULT_DISK_BUS,
    DEFAULT_MEMORY_GUEST,
    OWNER_LABEL,
    Cluster,
    KubeVirtControl,
    Settings,
    build_create_request,
    pick_root_disk_size,
)

VALID_ENV = {
    "HARBOR_KUBEVIRT_IMAGE": "/var/lib/kubevirt/custom-disks/minimal.raw",
    "HARBOR_KUBEVIRT_NAMESPACE": "default",
    "HARBOR_KUBEVIRT_NODE": "cpu-nat-391",
    "HARBOR_KUBEVIRT_DISK_BUS": "sata",
    "HARBOR_KUBEVIRT_START_TIMEOUT": "600",
    "HARBOR_KUBEVIRT_COMMAND_TIMEOUT": "3600",
    "HARBOR_KUBEVIRT_TRANSFER_TIMEOUT": "300",
}


@contextlib.contextmanager
def _pristine_harbor_env():
    saved = {}
    for key in list(os.environ):
        if key.startswith("HARBOR_KUBEVIRT_"):
            saved[key] = os.environ.pop(key)
    try:
        yield
    finally:
        os.environ.update(saved)


def make_settings(overrides=None):
    with _pristine_harbor_env():
        os.environ.update({**VALID_ENV, **(overrides or {})})
        return Settings.from_env()


def _cluster():
    return Cluster(
        api_server="https://10.254.64.34:6443",
        ca_data="CA",
        client_cert_data="CERT",
        client_key_data="KEY",
    )


class SettingsTests(unittest.TestCase):
    def test_requires_image(self):
        with self.assertRaises(ValueError):
            make_settings({"HARBOR_KUBEVIRT_IMAGE": ""})

    def test_default_namespace_and_waa_port(self):
        settings = make_settings()
        self.assertEqual(settings.namespace, "default")
        self.assertEqual(settings.waa_port, 5000)
        self.assertEqual(settings.disk_bus, "sata")

    def test_rejects_bad_namespace(self):
        with self.assertRaises(ValueError):
            make_settings({"HARBOR_KUBEVIRT_NAMESPACE": "Bad_NS"})

    def test_rejects_invalid_waa_ports(self):
        for port in ("0", "65536", "invalid"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                make_settings({"HARBOR_KUBEVIRT_WAA_PORT": port})

    def test_rejects_bad_disk_bus(self):
        with self.assertRaises(ValueError):
            make_settings({"HARBOR_KUBEVIRT_DISK_BUS": "ide"})


def build_settings():
    settings = make_settings()
    return settings


class CreateRequestTests(unittest.TestCase):
    def test_spec_reuses_host_disk_image_sata_bus(self):
        settings = build_settings()
        spec = build_create_request(settings, "trial-a1b2")
        self.assertEqual(spec["apiVersion"], "kubevirt.io/v1")
        self.assertEqual(spec["kind"], "VirtualMachine")
        self.assertEqual(spec["metadata"]["name"], "trial-a1b2")
        self.assertEqual(spec["metadata"]["namespace"], "default")
        self.assertFalse(spec["spec"]["running"])
        disks = spec["spec"]["template"]["spec"]["domain"]["devices"]["disks"]
        root = next(d for d in disks if d["name"] == "disk0")
        self.assertEqual(root["disk"]["bus"], DEFAULT_DISK_BUS)
        self.assertEqual(DEFAULT_DISK_BUS, "sata")
        volumes = spec["spec"]["template"]["spec"]["volumes"]
        host = next(v for v in volumes if v["name"] == "disk0")
        self.assertEqual(host["hostDisk"]["path"], settings.image)
        self.assertEqual(host["hostDisk"]["type"], "Disk")
        self.assertEqual(
            spec["spec"]["template"]["spec"]["nodeSelector"],
            {"kubernetes.io/hostname": "cpu-nat-391"},
        )

    def test_compute_resources_reach_spec(self):
        spec = build_create_request(
            build_settings(), "trial-a1b2", cpus=8, memory_mb=16384
        )
        domain = spec["spec"]["template"]["spec"]["domain"]
        self.assertEqual(domain["cpu"], {
            "cores": 8, "sockets": 1, "threads": 1,
        })
        self.assertEqual(domain["resources"]["limits"]["memory"], "16384Mi")

    def test_defaults(self):
        domain = build_create_request(build_settings(), "trial-a1b2")[
            "spec"]["template"]["spec"]["domain"]
        self.assertEqual(domain["cpu"]["cores"], DEFAULT_CPU_CORES)
        self.assertEqual(domain["resources"]["limits"]["memory"], DEFAULT_MEMORY_GUEST)

    def test_waa_port_declared_on_masquerade_interface(self):
        settings = build_settings()
        spec = build_create_request(settings, "trial-a1b2")
        interfaces = spec["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
        self.assertEqual(interfaces[0]["ports"], [
            {"name": "waa", "port": 5000, "protocol": "TCP"}
        ])

    def test_honors_disk_bus_override(self):
        spec = build_create_request(build_settings(), "trial-a1b2", disk_bus="virtio")
        root = next(
            d for d in spec["spec"]["template"]["spec"]["domain"]["devices"]["disks"]
            if d["name"] == "disk0"
        )
        self.assertEqual(root["disk"]["bus"], "virtio")

    def test_rejects_bad_disk_bus(self):
        with self.assertRaises(ValueError):
            build_create_request(build_settings(), "trial-a1b2", disk_bus="ide")

    def test_includes_owner_label_and_domain_label(self):
        spec = build_create_request(
            build_settings(), "trial-a1b2", {OWNER_LABEL: "abc"}
        )
        self.assertEqual(spec["metadata"]["labels"][OWNER_LABEL], "abc")
        self.assertEqual(spec["metadata"]["labels"]["kubevirt.io/domain"], "trial-a1b2")

    def test_rejects_bad_name(self):
        for bad in ("UPPER_CASE", "with space", "A", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_create_request(build_settings(), bad)


class KubeVirtControlTests(unittest.IsolatedAsyncioTestCase):
    def _control(self, handler) -> KubeVirtControl:
        transport = httpx.MockTransport(handler)
        return KubeVirtControl(build_settings(), transport=transport)

    def _vm_status(self, ip="10.90.2.25", ready=True, phase="Running"):
        return {
            "status": {
                "ready": ready, "printableStatus": "Running",
                "phase": phase, "interfaces": [{"name": "default", "ipAddress": ip}],
                "nodeName": "cpu-nat-391",
            }
        }

    async def test_create_posts_virtualmachine_cr(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/virtualmachines"):
                calls.append((request.method, request.url.path))
                return httpx.Response(201, json={"metadata": {"name": "trial-a1b2"}})
            return httpx.Response(404, json={})

        control = self._control(handler)
        async with control:
            meta = await control.create("trial-a1b2", {OWNER_LABEL: "abc"})
        self.assertEqual(meta.get("name"), "trial-a1b2")
        self.assertEqual(
            calls,
            [("POST", "/apis/kubevirt.io/v1/namespaces/default/virtualmachines")],
        )

    async def test_get_returns_ready_ip_from_vmi(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/virtualmachineinstances/" in path:
                return httpx.Response(200, json=self._vm_status())
            return httpx.Response(200, json={"metadata": {"labels": {"kubevirt.io/domain": "trial"}}, "status": {"ready": True, "printableStatus": "Running"}})

        control = self._control(handler)
        async with control:
            vm = await control.get("trial")
        self.assertEqual(vm["ip"], "10.90.2.25")
        self.assertTrue(vm["ready"])
        self.assertEqual(vm["status"], "Running")

    async def test_get_missing_vmi_still_returns_not_ready(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/virtualmachineinstances/" in request.url.path:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"metadata": {}, "status": {}})

        control = self._control(handler)
        async with control:
            vm = await control.get("trial")
        self.assertFalse(vm["ready"])
        self.assertEqual(vm["ip"], "")

    async def test_start_stop_use_subresource_paths(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={})

        control = self._control(handler)
        async with control:
            await control.start("trial")
            await control.stop("trial")
        self.assertEqual(
            [c[1] for c in calls],
            [
                "/apis/subresources.kubevirt.io/v1/namespaces/default/virtualmachines/trial/start",
                "/apis/subresources.kubevirt.io/v1/namespaces/default/virtualmachines/trial/stop",
            ],
        )

    async def test_delete_removes_vm_and_waa_service(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={})

        control = self._control(handler)
        async with control:
            await control.delete("trial")
        paths = [c[1] for c in calls]
        self.assertIn(
            "/apis/kubevirt.io/v1/namespaces/default/virtualmachines/trial", paths
        )
        self.assertTrue(any(p.endswith("/services/trial-waa") for p in paths))

    async def test_expose_waa_creates_nodeport_and_returns_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/services/trial-waa"):
                return httpx.Response(200, json={"spec": {"ports": [{"nodePort": 30050}]}})
            if "/virtualmachineinstances/" in request.url.path:
                return httpx.Response(200, json={"status": {"nodeName": "cpu-nat-391"}})
            if path == "/api/v1/nodes/cpu-nat-391":
                return httpx.Response(
                    200,
                    json={"status": {"addresses": [{"type": "InternalIP", "address": "10.254.73.30"}]}},
                )
            if request.method == "POST" and path.endswith("/services"):
                return httpx.Response(201, json={})
            return httpx.Response(404, json={})

        control = self._control(handler)
        async with control:
            endpoint = await control.expose_waa("trial")
        self.assertEqual(endpoint, "10.254.73.30:30050")

    async def test_ping_ok(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/version")
            return httpx.Response(200, json={"major": "1"})

        control = self._control(handler)
        async with control:
            self.assertTrue(await control.ping())

    async def test_ping_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        control = self._control(handler)
        async with control:
            self.assertFalse(await control.ping())

    def test_available_ips_none_for_native_kubevirt(self):
        # KubeVirt allocates pod IPs automatically; no pre-allocation.
        self.assertEqual(pick_root_disk_size("32Gi", "40Gi"), "32Gi")
        self.assertEqual(pick_root_disk_size("32Gi", None), "32Gi")


if __name__ == "__main__":
    unittest.main()