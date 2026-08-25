"""Tests for rollout listener startup preflight behavior."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "run_rl_rollout_server.sh"
ZELLIJ_HELPER = Path(__file__).resolve().parents[1] / "ensure_rl_job_zellij.sh"


class RolloutServerStartupTest(unittest.TestCase):
    def test_detached_listener_preserves_configured_path(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("TERM=xterm-256color bash -c", source)
        self.assertNotIn("TERM=xterm-256color bash -lc", source)
        self.assertIn("command -v python3", source)
        self.assertIn("exec \"$2\" rollout_remote_harbor.py", source)

    def test_detached_zellij_does_not_inherit_initialization_lock(self) -> None:
        source = ZELLIJ_HELPER.read_text(encoding="utf-8")

        self.assertIn('>/dev/null 2>&1 9>&- &', source)

    def test_stop_does_not_require_python(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            harbor_dir = root_path / "Harbor"
            bin_dir = root_path / "bin"
            harbor_dir.mkdir()
            bin_dir.mkdir()
            for name in ("dirname", "rm"):
                target = shutil.which(name)
                self.assertIsNotNone(target)
                os.symlink(target, bin_dir / name)
            (harbor_dir / "env.sh").write_text(
                f"RL_SERVER_PID_FILE={root_path / 'missing.pid'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(bin_dir),
                    "HARBOR_SCRIPT_DIR": str(harbor_dir),
                }
            )

            stopped = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--stop"],
                check=False,
                capture_output=True,
                env=env,
                text=True,
                timeout=15,
            )

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertFalse((bin_dir / "python3").exists())

    def test_detached_listener_uses_resolved_python(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            harbor_dir = root_path / "Harbor"
            bin_dir = root_path / "bin"
            harbor_dir.mkdir()
            bin_dir.mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]

            (bin_dir / "setsid").write_text(
                "#!/bin/sh\nexec \"$@\"\n",
                encoding="utf-8",
            )
            health_server = root_path / "health_server.py"
            health_server.write_text(
                "import os\n"
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                "    def log_message(self, _format, *args):\n"
                "        pass\n"
                "HTTPServer(('127.0.0.1', int(os.environ['RL_PORT'])), Handler).serve_forever()\n",
                encoding="utf-8",
            )
            (bin_dir / "python3").write_text(
                "#!/bin/sh\n"
                "case \"${1:-}\" in\n"
                "  *rollout_worker_utils.py) exit 0 ;;\n"
                "esac\n"
                f"exec {sys.executable!r} {str(health_server)!r}\n",
                encoding="utf-8",
            )
            (bin_dir / "setsid").chmod(0o755)
            (bin_dir / "python3").chmod(0o755)
            (harbor_dir / "env.sh").write_text(
                f"""
export RL_SERVER_PID_FILE={root_path / 'server.pid'}
export RL_TRIALS_DIR={root_path / 'trials'}
export RL_ACTIVE_DIR={root_path / 'queue' / 'active'}
export RL_QUEUE_DIR={root_path / 'queue'}
export RL_JOB_QUEUE_ROOT={root_path / 'queue' / 'jobs'}
export RL_JOB_RUNTIME_ROOT={root_path / 'runtime' / 'jobs'}
export RL_TRACE_LOG={root_path / 'runtime' / 'trace.jsonl'}
export RL_SERVER_LOG={root_path / 'runtime' / 'server.log'}
export RUNTIME_DIR={root_path / 'runtime'}
export RL_PORT={port}
harbor_prepare_agent_runtime() {{ return 0; }}
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}:/usr/bin:/bin",
                    "HARBOR_SCRIPT_DIR": str(harbor_dir),
                    "RL_AGENT": "claude-code",
                    "HARBOR_CC_OPIK_ENABLE_HOOK": "0",
                }
            )
            try:
                started = subprocess.run(
                    ["bash", str(SCRIPT), "--detach"],
                    check=False,
                    capture_output=True,
                    env=env,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertIn(f"port={port}", started.stdout)
            finally:
                subprocess.run(
                    ["bash", str(SCRIPT), "--stop"],
                    check=False,
                    capture_output=True,
                    env=env,
                    text=True,
                    timeout=15,
                )

    def _run_server_preflight(
        self,
        *,
        agent: str,
        trace_enabled: str,
        create_opencode_plugin: bool = False,
        create_opencode_hook: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            harbor_dir = root_path / "Harbor"
            harbor_dir.mkdir()
            opencode_plugin = root_path / "opik-trace.ts"
            opencode_hook = root_path / "opencode-realtime-trace.py"
            if create_opencode_plugin:
                opencode_plugin.touch()
            if create_opencode_hook:
                opencode_hook.touch()

            (harbor_dir / "env.sh").write_text(
                """
RL_SERVER_PID_FILE="$TEST_ROOT/server.pid"
RL_TRIALS_DIR="$TEST_ROOT/trials"
RL_ACTIVE_DIR="$TEST_ROOT/queue/active"
RL_QUEUE_DIR="$TEST_ROOT/queue"
RL_JOB_QUEUE_ROOT="$TEST_ROOT/queue/jobs"
RL_JOB_RUNTIME_ROOT="$TEST_ROOT/runtime/jobs"
RL_TRACE_LOG="$TEST_ROOT/runtime/trace.jsonl"
RL_SERVER_LOG="$TEST_ROOT/runtime/server.log"
RUNTIME_DIR="$TEST_ROOT/runtime"
RL_PORT=19001
harbor_prepare_agent_runtime() {
  echo prepare-called
  return 1
}
""",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "HARBOR_SCRIPT_DIR": str(harbor_dir),
                    "TEST_ROOT": str(root_path),
                    "RL_AGENT": agent,
                    "OPIK_URL": (
                        "https://opik.example.invalid/api"
                        if trace_enabled == "true"
                        else ""
                    ),
                    "HARBOR_CC_OPIK_ENABLE_HOOK": "0",
                    "TRACE_PLUGIN_CLAUDE_HOOK_SOURCE": str(
                        root_path / "missing-claude-hook.py"
                    ),
                    "TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE": str(opencode_plugin),
                    "TRACE_PLUGIN_OPENCODE_HOOK_SOURCE": str(opencode_hook),
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                check=False,
                capture_output=True,
                env=env,
                text=True,
            )
            return result, opencode_plugin, opencode_hook

    def test_trace_disabled_claude_reaches_runtime_preparation(self) -> None:
        result, _, _ = self._run_server_preflight(
            agent="claude-code",
            trace_enabled="false",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("prepare-called", result.stdout)
        self.assertNotIn("trace plugin source missing", result.stderr)

    def test_trace_disabled_opencode_requires_both_runtime_sources(self) -> None:
        result, _, opencode_hook = self._run_server_preflight(
            agent="opencode",
            trace_enabled="false",
            create_opencode_plugin=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("prepare-called", result.stdout)
        self.assertIn(f"trace plugin source missing: {opencode_hook}", result.stderr)

    def test_trace_disabled_opencode_with_sources_reaches_preparation(self) -> None:
        result, _, _ = self._run_server_preflight(
            agent="opencode",
            trace_enabled="false",
            create_opencode_plugin=True,
            create_opencode_hook=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("prepare-called", result.stdout)
        self.assertNotIn("trace plugin source missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
