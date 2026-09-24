"""Offline unit tests for the direct-IP SSH transport options."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kubevirt_windows.control import Platform, Settings
from kubevirt_windows.transport import WindowsSSH


def _settings(tmp_dir):
    key = Path(tmp_dir) / "ssh_key"
    key.write_text("unused key material", encoding="utf-8")
    return Settings(
        platform=Platform(base_url="http://10.9.202.91:31600", token="t"),
        image="ubuntu20.04-template-image",
        namespace="default",
        ssh_user="runner",
        ssh_key=key,
        subnet="ovn-default",
        storage_class="ceph-rbd-sc",
        ssh_port=2222,
    )


class WindowsSSHOptionsTests(unittest.TestCase):
    def test_options_connect_directly_to_vm_ip_without_virtctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = WindowsSSH(
                _settings(tmp), name="trial-x", ip="10.16.0.4", local_dir=tmp
            )
            opts = ssh.options()
        self.assertIn("-p", opts)
        idx = opts.index("-p")
        self.assertEqual(opts[idx + 1], "2222")
        self.assertIn("HostName=10.16.0.4", opts)
        joined = " ".join(opts)
        self.assertNotIn("ProxyCommand", joined)
        self.assertNotIn("virtctl", joined)
        self.assertNotIn("kubectl", joined)
        self.assertNotIn("port-forward", joined)

    def test_options_pin_per_trial_known_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = WindowsSSH(
                _settings(tmp), name="trial-x", ip="10.16.0.4", local_dir=tmp
            )
            opts = ssh.options()
        expected = f"UserKnownHostsFile={Path(tmp) / 'known_hosts'}"
        self.assertIn(expected, opts)

    def test_options_contains_ssh_user_and_port_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            ssh = WindowsSSH(
                settings, name="trial-x", ip="10.16.0.4", local_dir=tmp
            )
            opts = ssh.options()
        self.assertIn("User=runner", opts)
        self.assertIn(str(settings.ssh_key), opts)
        self.assertIn("BatchMode=yes", opts)
        self.assertIn("ConnectionAttempts=1", opts)