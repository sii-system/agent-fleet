"""Offline unit tests for the KubeVirt-native lifecycle and request builder."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kubevirt_windows.control import (
    DEFAULT_CPU_CORES,
    DEFAULT_DISK_BUS,
    DEFAULT_MEMORY_GUEST,
    OWNER_LABEL,
    KubeVirtControl,
    Settings,
    build_create_request,
)

VALID_ENV = {
    "HARBOR_KUBEVIRT_IMAGE": "windows-golden",
    "HARBOR_KUBEVIRT_NAMESPACE": "default",
    "HARBOR_KUBEVIRT_NODE": "cpu-nat-391",
    "HARBOR_KUBEVIRT_DISK_BUS": "sata",
    "HARBOR_KUBEVIRT_START_TIMEOUT": "600",
    "HARBOR_KUBEVIRT_COMMAND_TIMEOUT": "3600",
    "HARBOR_KUBEVIRT_TRANSFER_TIMEOUT": "300",
}


@contextlib.contextmanager
def _pristine_harbor_env():
    with patch.dict(os.environ):
        for key in list(os.environ):
            if key.startswith("HARBOR_KUBEVIRT_"):
                del os.environ[key]
        yield


def make_settings(overrides=None):
    with _pristine_harbor_env(), patch(
        "kubevirt_windows.control._load_kubeconfig",
        return_value=("https://cluster.example", "fake-ca", "fake-cert", "fake-key"),
    ):
        os.environ.update({**VALID_ENV, **(overrides or {})})
        return Settings.from_env()


class SettingsTests(unittest.TestCase):
    def test_settings_do_not_read_operator_files_or_leak_environment(self):
        before = dict(os.environ)
        with patch("pathlib.Path.open", side_effect=AssertionError("Unexpected file read")):
            self.assertEqual(make_settings().image, "windows-golden")
        self.assertEqual(dict(os.environ), before)

    def test_rejects_legacy_host_disk_path(self):
        with self.assertRaisesRegex(ValueError, "golden-image PVC"):
            make_settings({"HARBOR_KUBEVIRT_IMAGE": "/images/golden.raw"})

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
    def test_trials_attach_distinct_clones_and_never_the_source(self):
        settings = build_settings()
        requests = [build_create_request(settings, name) for name in ("trial-a", "trial-b")]
        attached = []
        for request in requests:
            spec = request["spec"]
            volume = spec["template"]["spec"]["volumes"][0]
            self.assertEqual(set(volume), {"name", "dataVolume"})
            attached.append(volume["dataVolume"]["name"])
            self.assertEqual(attached[-1], spec["dataVolumeTemplates"][0]["metadata"]["name"])
            self.assertNotEqual(attached[-1], settings.image)
        self.assertNotEqual(*attached)

    def test_storage_class_override_reaches_clone(self):
        settings = make_settings({"HARBOR_KUBEVIRT_STORAGE_CLASS": "windows-storage"})
        spec = build_create_request(settings, "trial-a")["spec"]
        self.assertEqual(spec["dataVolumeTemplates"][0]["spec"]["storage"], {
            "storageClassName": "windows-storage",
        })

    def test_spec_clones_pvc_with_sata_bus(self):
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
        self.assertEqual(host["dataVolume"]["name"], "trial-a1b2-root")
        clone = spec["spec"]["dataVolumeTemplates"][0]
        self.assertEqual(clone["metadata"]["name"], host["dataVolume"]["name"])
        self.assertEqual(clone["spec"]["source"], {
            "pvc": {"name": settings.image, "namespace": settings.namespace},
        })
        self.assertEqual(clone["spec"]["storage"], {})
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
    async def test_service_cleanup_is_idempotent_when_missing(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(404)

        async with self._control(handler) as control:
            await control.delete_service("trial", owner="abc")
        self.assertEqual(calls, ["GET"])

    async def test_service_cleanup_refuses_another_owner(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"metadata": {
                "uid": "other-uid", "labels": {OWNER_LABEL: "other"},
            }})

        async with self._control(handler) as control:
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                await control.delete_service("trial", owner="abc")
        self.assertEqual(calls, ["GET"])

    async def test_service_cleanup_binds_delete_to_observed_uid(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"metadata": {
                    "uid": "svc-uid", "labels": {OWNER_LABEL: "abc"},
                }})
            self.assertEqual(json.loads(request.content), {
                "preconditions": {"uid": "svc-uid"},
            })
            return httpx.Response(200)

        async with self._control(handler) as control:
            await control.delete_service("trial", owner="abc")

    def _control(self, handler) -> KubeVirtControl:
        transport = httpx.MockTransport(handler)
        control = KubeVirtControl(build_settings())
        # Inject the HTTP boundary: no TLS material or operator kubeconfig is read.
        control._client = httpx.AsyncClient(
            base_url=control.settings.cluster.api_server, transport=transport,
            trust_env=False,
        )
        return control

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
            return httpx.Response(200, json={"metadata": {
                "uid": "svc-uid", "labels": {OWNER_LABEL: "abc"},
            }})

        control = self._control(handler)
        async with control:
            await control.delete("trial", owner="abc")
        paths = [c[1] for c in calls]
        self.assertIn(
            "/apis/kubevirt.io/v1/namespaces/default/virtualmachines/trial", paths
        )
        self.assertTrue(any(p.endswith("/services/trial-waa") for p in paths))

    async def test_expose_waa_creates_nodeport_and_returns_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/services/trial-waa"):
                return httpx.Response(200, json={
                    "metadata": {"labels": {OWNER_LABEL: "abc"}},
                    "spec": {"ports": [{"nodePort": 30050}]},
                })
            if "/virtualmachineinstances/" in request.url.path:
                return httpx.Response(200, json={"status": {"nodeName": "cpu-nat-391"}})
            if path == "/api/v1/nodes/cpu-nat-391":
                return httpx.Response(
                    200,
                    json={"status": {"addresses": [{"type": "InternalIP", "address": "10.254.73.30"}]}},
                )
            if "/virtualmachines/" in path:
                return httpx.Response(200, json={"metadata": {
                    "uid": "vm-uid", "labels": {OWNER_LABEL: "abc"},
                }})
            if request.method == "POST" and path.endswith("/services"):
                body = json.loads(request.content)
                self.assertEqual(body["metadata"]["ownerReferences"][0]["uid"], "vm-uid")
                return httpx.Response(201, json={})
            return httpx.Response(404, json={})

        control = self._control(handler)
        async with control:
            endpoint = await control.expose_waa("trial", owner="abc")
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



if __name__ == "__main__":
    unittest.main()
