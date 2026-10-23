from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class DependencyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.runtime = self.root / "runtime"
        self.scripts = self.root / "scripts"
        for directory in (self.cache, self.runtime, self.scripts):
            directory.mkdir()
        (self.cache / "manifest.txt").write_text(
            "cache_schema=3\nclaude_npm_cache_version=1.2.3\n", encoding="utf-8"
        )
        (self.cache / "opik-fixture.whl").touch()
        (self.cache / "get-pip.py").touch()
        (self.cache / "npm-cache" / "_cacache").mkdir(parents=True)
        (self.cache / "npm-cache-ready").write_text("1.2.3\n", encoding="utf-8")
        for name in ("claude-code-test.tgz", "opencode-ai-test.tgz", "opencode-linux-x64-test.tgz"):
            self._write_archive(self.cache / name)
        self._write_archive(self.cache / "node-runtime.tar.xz", "w:xz")
        self.node_fixture = self.root / "node-runtime.tar.gz"
        self._write_archive(self.node_fixture)
        (self.scripts / "env.py").symlink_to(HARBOR_DIR / "env.py")
        preparer = self.scripts / "prepare_local_deps.sh"
        preparer.write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            'cp "$NODE_FIXTURE" "$WHEEL_DIR/node-runtime.tar.gz"\n'
            'rm -f "$WHEEL_DIR/node-runtime.tar.xz"\n',
            encoding="utf-8",
        )
        preparer.chmod(0o755)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(self.cache))
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    @staticmethod
    def _write_archive(path: Path, mode: str = "w:gz") -> None:
        payload = json.dumps({"version": "1.2.3"}).encode()
        with tarfile.open(path, mode) as archive:
            member = tarfile.TarInfo("package/package.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    def _run_shell(self, command: str, agent: str = "opencode") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "AGENT": agent,
                "DEPENDENCIES_SH": str(HARBOR_DIR / "env" / "dependencies.sh"),
                "BACKEND_PLAN_PY": str(HARBOR_DIR / "backend_plan.py"),
                "SCRIPT_DIR": str(self.scripts),
                "NODE_FIXTURE": str(self.node_fixture),
                "LOCAL_WHEEL_DIR": str(self.cache),
                "RUNTIME_DIR": str(self.runtime),
                "WORKERS_READY_FILE": str(self.runtime / "ready"),
                "WORKERS_FAILED_FILE": str(self.runtime / "failed"),
                "LOCAL_DEPS_LOG_FILE": str(self.runtime / "prepare.log"),
                "EFFECTIVE_WHEEL_URL_FILE": str(self.runtime / "wheel-url"),
                "EFFECTIVE_CLAUDE_TGZ_URL_FILE": str(self.runtime / "claude-url"),
                "HARBOR_RUNNER_PREPARE_STATUS_FILE": str(self.runtime / "runner-status"),
                "HARBOR_LOCAL_WHEEL_SERVER_URL": f"http://127.0.0.1:{self.server.server_port}",
                "HARBOR_REMOTE_WHEEL_SERVER_URLS": "",
                "HARBOR_ENVIRONMENT_TYPE": "opensandbox",
                "YICLOUD_SANDBOX_UPLOAD_BACKEND": "s3",
                "LOCAL_WHEEL_PORT": str(self.server.server_port),
                "LOCAL_WHEEL_PORT_ATTEMPTS": "1",
                "LOCAL_WHEEL_HOST_IP": "127.0.0.1",
                "CLAUDE_CODE_VERSION": "1.2.3",
                "CLAUDE_CODE_TGZ_BASENAME": "claude-code-test.tgz",
                "OPENCODE_VERSION": "1.2.3",
                "OPENCODE_TGZ_BASENAME": "opencode-ai-test.tgz",
                "OPENCODE_LINUX_X64_TGZ_BASENAME": "opencode-linux-x64-test.tgz",
                "PI_VERSION": "0.81.1",
                "PI_TGZ_BASENAME": "pi-test.tgz",
                "PI_NODE_RUNTIME_BASENAME": "pi-node-runtime.tar.gz",
                "PI_RUNTIME_BASENAME": "pi-runtime-test.tar.gz",
                "PYTHON_BIN": sys.executable,
            }
        )
        script = r'''
set -euo pipefail
eval "$("$PYTHON_BIN" -S "$BACKEND_PLAN_PY")"
source "$DEPENDENCIES_SH"
harbor_agent_is_claude_code() { [[ "$AGENT" == claude-code ]]; }
harbor_agent_is_opencode() { [[ "$AGENT" == opencode ]]; }
harbor_agent_is_pi() { return 1; }
harbor_agent_is_dsh() { return 1; }
harbor_agent_tgz_basename() {
  if harbor_agent_is_opencode; then printf '%s\n' "$OPENCODE_TGZ_BASENAME";
  else printf '%s\n' "$CLAUDE_CODE_TGZ_BASENAME"; fi
}
harbor_python_runtime_archive_ready() { return 0; }
validate_verifier_runtime_bundle_transport() { return 0; }
harbor_prepare_web_mcp() { return 0; }
harbor_build_verifier_runtime_bundle() { return 0; }
harbor_prewarm_s3_upload_cache() { harbor_tar_file_ready "$LOCAL_WHEEL_DIR/node-runtime.tar.gz"; }
harbor_mark_workers_ready() { touch "$WORKERS_READY_FILE"; }
'''
        return subprocess.run(
            ["bash", "-c", script + command],
            env=environment, capture_output=True, text=True, check=False, timeout=15,
        )

    def test_old_server_does_not_bypass_local_preparation(self) -> None:
        for agent in ("claude-code", "opencode"):
            with self.subTest(agent=agent):
                (self.cache / "node-runtime.tar.gz").unlink(missing_ok=True)
                self._write_archive(self.cache / "node-runtime.tar.xz", "w:xz")
                result = self._run_shell(
                    "harbor_prepare_or_select_wheels\nharbor_local_cache_ready\n", agent
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (self.runtime / "local-deps-prepare.status").read_text().strip(), "done"
                )
                self.assertTrue((self.runtime / "ready").is_file())
                self.assertFalse((self.runtime / "failed").exists())

    def test_existing_server_requires_gzip_runtime(self) -> None:
        stale = self._run_shell("harbor_ensure_local_wheels_server\n")
        self.assertNotEqual(stale.returncode, 0)

        self._write_archive(self.cache / "node-runtime.tar.gz")
        ready = self._run_shell("harbor_ensure_local_wheels_server\n")
        self.assertEqual(ready.returncode, 0, ready.stderr)

    def test_remote_cache_with_gzip_runtime_remains_eligible(self) -> None:
        self._write_archive(self.cache / "node-runtime.tar.gz")
        result = self._run_shell("harbor_pick_remote_wheel_url\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"http://127.0.0.1:{self.server.server_port}")


if __name__ == "__main__":
    unittest.main()
