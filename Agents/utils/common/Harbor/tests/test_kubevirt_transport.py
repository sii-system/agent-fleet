"""Offline unit tests for the direct-IP SSH transport options."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kubevirt_windows.control import Platform, Settings
from kubevirt_windows.transport import WindowsSSH


def _settings(tmp_dir):
    key = Path(tmp_dir) / "ssh_key"
    key.write_text("unused key material", encoding="utf-8")
    return Settings(
        platform=Platform(base_url="https://vm-platform.example.com", token="t"),
        image="windows-benchmark-v1",
        namespace="default",
        ssh_user="runner",
        ssh_key=key,
        subnet="ovn-default",
        storage_class="ceph-rbd-sc",
        ssh_port=2222,
    )


class WindowsSSHOptionsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sftp"), "OpenSSH sftp is required")
    def test_sftp_parses_shared_options_and_forwards_configured_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "args.txt"
            stub = root / "ssh-stub"
            stub.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['SSH_CAPTURE']).write_text('\\n'.join(sys.argv[1:]))\n"
                "sys.exit(1)\n"
            )
            stub.chmod(0o755)
            ssh = WindowsSSH(_settings(tmp), "trial-x", "192.0.2.4", tmp)
            subprocess.run(
                ["sftp", "-S", str(stub), *ssh.options(), "-b", "-", "trial-x"],
                input="",
                text=True,
                capture_output=True,
                timeout=5,
                env={**os.environ, "SSH_CAPTURE": str(capture)},
                check=False,
            )
            forwarded = capture.read_text().splitlines()
        self.assertIn("Port=2222", forwarded)
        self.assertIn("trial-x", forwarded)

    def test_options_connect_directly_to_vm_ip_without_virtctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = WindowsSSH(
                _settings(tmp), name="trial-x", ip="10.16.0.4", local_dir=tmp
            )
            opts = ssh.options()
        self.assertIn("Port=2222", opts)
        self.assertNotIn("-p", opts)  # sftp uses -p for permissions, not port.
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
            ssh = WindowsSSH(settings, name="trial-x", ip="10.16.0.4", local_dir=tmp)
            opts = ssh.options()
        self.assertIn("User=runner", opts)
        self.assertIn(str(settings.ssh_key), opts)
        self.assertIn("BatchMode=yes", opts)
        self.assertIn("ConnectionAttempts=1", opts)
