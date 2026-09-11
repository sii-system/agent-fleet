import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


class FrontendGoTest(unittest.TestCase):
    def run_shell(self, root, body, **env):
        return subprocess.run(
            ['/bin/bash', '-c', 'source "$1/prerequisites.sh"\n' + body, 'test', str(SCRIPTS)],
            env={**os.environ, 'AGENT_FLEET_PATHS_FILE': str(root / 'absent'),
                 'AGENT_FLEET_BIN_DIR': str(root / 'bin'), 'AGENT_FLEET_CACHE_DIR': str(root / 'cache'),
                 'REPO_DIR': str(root), 'PATH': '/usr/bin:/bin', **env},
            capture_output=True, text=True, timeout=20, check=False,
        )

    def test_compiler_selection_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bin').mkdir()
            for version, status in [('1.20.1', 1), ('1.25.4', 0)]:
                binary = root / 'bin/go'
                binary.write_text(f'#!/bin/sh\necho go version go{version} linux/amd64\n')
                binary.chmod(0o755)
                result = self.run_shell(root, 'agent_fleet_find_frontend_go')
                self.assertEqual(result.returncode, status, result.stderr)
            result = self.run_shell(root, 'agent_fleet_download() { exit 99; }; agent_fleet_install_frontend_go')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(binary))

    def test_bad_checksum_never_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_shell(root, '''agent_fleet_find_frontend_go() { return 1; }
uname() { if [[ "$1" == -s ]]; then echo Linux; else echo x86_64; fi; }
agent_fleet_download() { printf corrupt > "$2"; }
agent_fleet_install_frontend_go
''', ARTIFACT_CACHE_GATEWAY_URL='http://gateway.invalid/v1/cache')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'bin/go').exists())

    def test_setup_selection(self):
        for backend, enabled, managed, expected in (
            ('docker', 'auto', '1', ''), ('opensandbox', 'auto', '1', 'install'),
            ('docker', '1', '1', 'install'), ('opensandbox', '0', '1', ''),
            ('opensandbox', '1', '0', 'check'),
        ):
            with (self.subTest(backend=backend, enabled=enabled, managed=managed),
                  tempfile.TemporaryDirectory() as tmp):
                body = '\n'.join(f'{name}() {{ :; }}' for name in (
                    'agent_fleet_prerequisite_init_path', 'agent_fleet_prerequisite_init_runtime',
                    'agent_fleet_check_core', 'agent_fleet_check_harbor', 'agent_fleet_install_zellij',
                    'agent_fleet_install_uv', 'agent_fleet_save_prerequisite_paths'))
                body += '\nagent_fleet_install_frontend_go() { echo install; }\nagent_fleet_find_frontend_go() { echo check; }\nagent_fleet_bootstrap_setup_prerequisites'
                result = self.run_shell(Path(tmp), body, HARBOR_ENVIRONMENT_TYPE=backend,
                                        HARBOR_OPENSANDBOX_BUILD_TOOLS_SETUP=enabled,
                                        AGENT_FLEET_PREREQUISITES_INSTALL_MANAGED=managed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
