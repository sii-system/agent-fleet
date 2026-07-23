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
    def test_quotes_append_system_prompt_when_opik_hook_is_disabled(self) -> None:
        module = load_module()
        captured: list[str] = []

        class FakeClaudeCode:
            async def install(self, environment):
                return None

            async def run(self, instruction, environment, context):
                command = (
                    "claude --append-system-prompt Use English only for all reasoning. "
                    "--permission-mode=bypassPermissions --print -- 'real task'"
                )
                return await self.exec_as_agent(environment, command)

            async def exec_as_agent(
                self, environment, command, env=None, cwd=None, timeout_sec=None
            ):
                captured.append(command)
                return command

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
            agent._extra_env = {"CC_OPIK_ENABLE_HOOK": "false"}
            asyncio.run(agent.run("real task", object(), object()))

        self.assertEqual(len(captured), 1)
        argv = shlex.split(captured[0])
        prompt_index = argv.index("--append-system-prompt")
        self.assertEqual(argv[prompt_index + 1], "Use English only for all reasoning.")
        self.assertEqual(argv[-1], "real task")


if __name__ == "__main__":
    unittest.main()
