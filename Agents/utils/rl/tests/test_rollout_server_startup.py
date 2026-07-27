"""Tests for rollout listener startup preflight behavior."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "run_rl_rollout_server.sh"


class RolloutServerStartupTest(unittest.TestCase):
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
                    "TRACE_TO_OPIK": trace_enabled,
                    "TB_TRACE_TO_OPIK": trace_enabled,
                    "TB_CC_OPIK_ENABLE_HOOK": "0",
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
