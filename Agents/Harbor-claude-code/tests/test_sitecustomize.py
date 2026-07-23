#!/usr/bin/env python3
"""Regression tests for Claude Code command compatibility patches."""

from __future__ import annotations

import asyncio
import importlib.util
import shlex
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).parents[1]
SCRIPT = SCRIPT_DIR / "sitecustomize.py"
HARBOR_RUNTIME_DIR = SCRIPT_DIR.parent / "utils" / "common" / "Harbor"


def load_module():
    import_paths = [str(SCRIPT_DIR), str(HARBOR_RUNTIME_DIR)]
    sys.path[:0] = import_paths
    try:
        spec = importlib.util.spec_from_file_location("claude_sitecustomize_test", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for import_path in import_paths:
            sys.path.remove(import_path)


class ClaudeCommandPatchTest(unittest.TestCase):
    def test_file_backs_append_system_prompt_when_opik_hook_is_disabled(self) -> None:
        module = load_module()
        prompt = "Use English only for all reasoning. $(touch /tmp/must-not-run)"

        class FakeEnvironment:
            def __init__(self) -> None:
                self.uploads: list[tuple[str, bytes]] = []
                self.commands: list[tuple[str | None, str]] = []

            async def upload_file(self, source_path, target_path) -> None:
                self.uploads.append((target_path, Path(source_path).read_bytes()))

            async def exec(
                self,
                command,
                cwd=None,
                env=None,
                timeout_sec=None,
                user=None,
            ):
                self.commands.append((user, command))
                return SimpleNamespace(stdout="", return_code=0)

        class FakeLogger:
            def debug(self, *args, **kwargs) -> None:
                return None

            def warning(self, *args, **kwargs) -> None:
                return None

        class FakeClaudeCode:
            def __init__(self) -> None:
                self._resolved_flags = {"append_system_prompt": prompt}
                self._extra_env = {"CC_OPIK_ENABLE_HOOK": "false"}
                self.logger = FakeLogger()

            async def install(self, environment):
                return None

            async def run(self, instruction, environment, context):
                command = "claude --verbose --output-format=stream-json"
                configured_prompt = self._resolved_flags.get("append_system_prompt")
                if configured_prompt is not None:
                    command += f" --append-system-prompt {configured_prompt}"
                command += (
                    " --permission-mode=bypassPermissions --print -- stale-task "
                    "2>&1 </dev/null | tee /logs/agent/claude-code.txt"
                )
                return await self.exec_as_agent(environment, command)

            async def exec_as_agent(
                self, environment, command, env=None, cwd=None, timeout_sec=None
            ):
                if "id -u" in command and "id -g" in command:
                    return SimpleNamespace(stdout="1000 1000\n", return_code=0)
                return await environment.exec(
                    command=command,
                    cwd=cwd,
                    env=env,
                    timeout_sec=timeout_sec,
                    user="agent",
                )

            async def exec_as_root(
                self, environment, command, env=None, cwd=None, timeout_sec=None
            ):
                return await environment.exec(
                    command=command,
                    cwd=cwd,
                    env=env,
                    timeout_sec=timeout_sec,
                    user="root",
                )

        claude_code = types.ModuleType("harbor.agents.installed.claude_code")
        claude_code.ClaudeCode = FakeClaudeCode
        fake_modules = {
            name: types.ModuleType(name)
            for name in ("harbor", "harbor.agents", "harbor.agents.installed")
        }
        fake_modules.update({
            "harbor.agents.installed.claude_code": claude_code,
        })

        with mock.patch.dict(sys.modules, fake_modules):
            module._patch_claude_code_realtime_hooks()
            agent = FakeClaudeCode()
            environment = FakeEnvironment()
            saved_flags = agent._resolved_flags
            asyncio.run(agent.run("real task", environment, object()))

        self.assertIs(agent._resolved_flags, saved_flags)
        self.assertEqual(environment.uploads[0][1], prompt.encode("utf-8"))
        all_commands = "\n".join(command for _user, command in environment.commands)
        self.assertNotIn(prompt, all_commands)
        claude_commands = [
            command
            for _user, command in environment.commands
            if "claude --verbose --output-format=stream-json" in command
        ]
        self.assertEqual(len(claude_commands), 1)
        argv = shlex.split(claude_commands[0].split("; __tb_pipeline_status", 1)[0])
        self.assertIn("--append-system-prompt-file", argv)
        self.assertNotIn("--append-system-prompt", argv)


if __name__ == "__main__":
    unittest.main()
