"""Exercise legacy Harbor import paths with the installed Harbor/E2B APIs."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]


class EnvironmentAdapterImportTests(unittest.TestCase):
    def run_import_check(self, script):
        result = subprocess.run(
            [sys.executable, "-S", "-c", script],
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_and_canonical_classes_share_the_real_harbor_contract(self):
        # Preserve the installed dependencies while suppressing startup hooks.
        self.run_import_check(f"""
import sys
sys.path[:] = {sys.path!r}
sys.path.insert(0, {str(HARBOR_DIR)!r})
import inspect
from harbor.environments.base import BaseEnvironment
from harbor.environments.e2b import E2BEnvironment
from harbor_environment_adapters.providers.opensandbox_yicloud.adapter import YiCloudOpenSandboxEnvironment
from harbor_environment_adapters.providers.dsec_controller.adapter import DsecSandboxEnvironment
from harbor_environment_adapters.providers.e2b_series.qz_legacy.adapter import QzSandboxEnvironment
from dsec_sandbox import DsecSandboxEnvironment as LegacyDsec
from yicloud_opensandbox import YiCloudOpenSandboxEnvironment as LegacyYiCloud
from qz_e2b_sandbox import QzSandboxEnvironment as LegacyQz
assert LegacyYiCloud is YiCloudOpenSandboxEnvironment
assert LegacyDsec is DsecSandboxEnvironment
assert LegacyQz is QzSandboxEnvironment
assert issubclass(LegacyYiCloud, BaseEnvironment)
assert issubclass(LegacyDsec, BaseEnvironment)
assert issubclass(LegacyQz, E2BEnvironment)
assert not inspect.isabstract(LegacyYiCloud)
assert not inspect.isabstract(LegacyDsec)
assert not inspect.isabstract(LegacyQz)
assert "yicloud" not in sys.modules
""")

    def test_importing_qz_legacy_does_not_patch_the_official_sdk(self):
        self.run_import_check(f"""
import sys
sys.path[:] = {sys.path!r}
sys.path.insert(0, {str(HARBOR_DIR)!r})
import os
from e2b.connection_config import ConnectionConfig
original = ConnectionConfig.get_host
os.environ.update(E2B_API_KEY="fake-cloud-key", E2B_API_URL="https://e2b.example.invalid")
before = dict(os.environ)
from qz_e2b_sandbox import apply_qz_environment, patch_envd_host
from harbor_environment_adapters.providers.e2b_series.qz_legacy import adapter
assert apply_qz_environment is adapter.apply_qz_environment
assert patch_envd_host is adapter.patch_envd_host
assert ConnectionConfig.get_host is original
assert dict(os.environ) == before
""")
