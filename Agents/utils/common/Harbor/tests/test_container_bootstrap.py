from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

HARBOR_RUNTIME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_RUNTIME_DIR))
try:
    import container_bootstrap
finally:
    sys.path.remove(str(HARBOR_RUNTIME_DIR))


def assert_valid_bash(test: unittest.TestCase, command: str) -> None:
    result = subprocess.run(
        ["bash", "-n"],
        input=command,
        text=True,
        capture_output=True,
        check=False,
    )
    test.assertEqual(result.returncode, 0, result.stderr)


class ContainerBootstrapImportTest(unittest.TestCase):
    def test_shared_module_is_importable_from_harbor_runtime_path(self) -> None:
        sys.path.insert(0, str(HARBOR_RUNTIME_DIR))
        try:
            spec = importlib.util.find_spec("container_bootstrap")
        finally:
            sys.path.remove(str(HARBOR_RUNTIME_DIR))

        self.assertIsNotNone(spec)

    def test_shared_module_exposes_bootstrap_builders(self) -> None:
        sys.path.insert(0, str(HARBOR_RUNTIME_DIR))
        try:
            import container_bootstrap
        finally:
            sys.path.remove(str(HARBOR_RUNTIME_DIR))

        self.assertTrue(hasattr(container_bootstrap, "NpmToolSpec"))
        self.assertTrue(hasattr(container_bootstrap, "build_python_runtime_command"))
        self.assertTrue(hasattr(container_bootstrap, "build_npm_tool_install_command"))
        self.assertTrue(
            hasattr(container_bootstrap, "build_python_dependencies_command")
        )


class PythonRuntimeCommandTest(unittest.TestCase):
    def test_prefers_valid_python312_cache_before_ipv4_package_fallback(self) -> None:
        command = container_bootstrap.build_python_runtime_command(
            "/opt/tb wheels"
        )

        self.assertIn("python3.12-runtime.tar.gz", command)
        self.assertIn("tar -xzf", command)
        self.assertIn("/opt/python3.12-runtime/bin/python3.12", command)
        self.assertIn("Acquire::ForceIPv4=true", command)
        self.assertIn("rm -f /usr/local/bin/python3 /usr/local/bin/python3.12", command)
        self.assertLess(
            command.index("python3.12-runtime.tar.gz"),
            command.index("apt-get"),
        )
        self.assertIn("wheel_dir='/opt/tb wheels'", command)
        assert_valid_bash(self, command)


class NpmToolInstallCommandTest(unittest.TestCase):
    def test_uses_shared_node_cache_offline_npm_and_ipv4_fallback(self) -> None:
        spec = container_bootstrap.NpmToolSpec(
            executable="claude",
            package="@anthropic-ai/claude-code",
            version="2.1.90",
            archive_path="/opt/tb-opik/claude-code.tgz",
            archive_url="https://cache.example/claude-code.tgz",
            archive_basename="claude-code.tgz",
            npm_cache_dir="/opt/tb-opik/python-wheels/npm-cache",
            npm_registry="https://registry.example",
        )

        command = container_bootstrap.build_npm_tool_install_command(
            spec,
            wheel_dir="/opt/tb-opik/python-wheels",
            wheel_url="https://cache.example/wheels",
            node_dist_url="https://cache.example/node.tar.gz",
        )

        self.assertIn("download_file()", command)
        self.assertIn("extract_archive()", command)
        self.assertIn('if extract_archive "$node_tgz" "$node_dir"; then', command)
        self.assertIn(
            'if download_file "$node_dist_url" "$node_dist_tgz" '
            '&& [ -s "$node_dist_tgz" ]; then',
            command,
        )
        self.assertIn("Acquire::ForceIPv4=true", command)
        self.assertIn('cp -a "$npm_cache_dir"/. "$npm_cache_tmp"/', command)
        self.assertIn('npm install -g --offline --cache "$npm_cache_tmp"', command)
        self.assertIn("@anthropic-ai/claude-code@2.1.90", command)
        self.assertLess(
            command.index('npm install -g "$tool_tgz"'),
            command.index("@anthropic-ai/claude-code@2.1.90"),
        )
        assert_valid_bash(self, command)

    def test_supports_platform_archive_for_opencode(self) -> None:
        spec = container_bootstrap.NpmToolSpec(
            executable="opencode",
            package="opencode-ai",
            version="1.2.3",
            archive_path="/cache/opencode-ai-1.2.3.tgz",
            archive_basename="opencode-ai-1.2.3.tgz",
            platform_archive_path="/cache/opencode-linux-x64-1.2.3.tgz",
            platform_archive_basename="opencode-linux-x64-1.2.3.tgz",
            npm_cache_dir="/cache/npm-cache",
        )

        command = container_bootstrap.build_npm_tool_install_command(
            spec,
            wheel_dir="/cache",
            wheel_url="",
            node_dist_url="",
        )

        self.assertIn("use_platform_archive=0", command)
        self.assertIn("uname -m", command)
        self.assertIn("glibc\\|GNU libc", command)
        self.assertIn('npm install -g "$tool_tgz" "$platform_tgz"', command)
        assert_valid_bash(self, command)


class PythonDependenciesCommandTest(unittest.TestCase):
    def test_handles_pep668_get_pip_sources_and_network_retries(self) -> None:
        command = container_bootstrap.build_python_dependencies_command(
            ("opik", "uuid6", "socksio"),
            wheel_dir="/opt/tb-opik/python-wheels",
            wheel_url="https://cache.example/wheels",
        )
        normalized = " ".join(command.replace("\\\n", "").split())

        self.assertIn("mods = ('opik', 'uuid6', 'socksio')", command)
        self.assertIn("export PIP_BREAK_SYSTEM_PACKAGES=1", command)
        self.assertIn("$wheel_dir/get-pip.py", command)
        self.assertIn("command -v curl", command)
        self.assertIn("command -v wget", command)
        self.assertIn("urllib.request.urlretrieve", command)
        self.assertIn("--user $pip_opts pip setuptools wheel", normalized)
        self.assertIn(
            "--break-system-packages $pip_opts pip setuptools wheel", normalized
        )
        self.assertIn("pip install --help", command)
        self.assertIn("--retries 10 --timeout 120", command)
        assert_valid_bash(self, command)


if __name__ == "__main__":
    unittest.main()
