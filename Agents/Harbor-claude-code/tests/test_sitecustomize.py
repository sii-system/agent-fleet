"""Regression tests for Claude Code command compatibility patches."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shlex
import subprocess
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
    def test_opik_hook_requires_shared_trace_gate(self) -> None:
        module = load_module()
        endpoint = "https://opik.example.invalid/api"

        self.assertFalse(
            module._hook_enabled(
                {"CC_OPIK_ENABLE_HOOK": "true", "OPIK_URL": ""}
            )
        )
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                self.assertFalse(
                    module._hook_enabled(
                        {
                            "CC_OPIK_ENABLE_HOOK": "true",
                            "OPIK_URL": endpoint,
                            "OPIK_TRACK_DISABLE": value,
                        }
                    )
                )
        self.assertTrue(
            module._hook_enabled(
                {"CC_OPIK_ENABLE_HOOK": "true", "OPIK_URL": endpoint}
            )
        )

    def test_quotes_append_system_prompt_when_opik_hook_is_disabled(self) -> None:
        module = load_module()
        captured: list[str] = []

        class FakeClaudeCode:
            async def install(self, environment):
                return None

            async def run(self, instruction, environment, context):
                command = (
                    "claude --verbose --output-format=stream-json "
                    "--append-system-prompt Use English only for all reasoning. "
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
        self.assertIn('export PATH="$HOME/.local/bin:$PATH";', captured[0])
        argv = shlex.split(captured[0])
        prompt_index = argv.index("--append-system-prompt")
        self.assertEqual(argv[prompt_index + 1], "Use English only for all reasoning.")
        self.assertEqual(argv[-1], "real task")


class ClaudeInstallCommandTest(unittest.TestCase):
    def _install_commands(self, extra_env: dict[str, str]) -> list[str]:
        module = load_module()
        captured: list[str] = []

        class FakeClaudeCode:
            async def install(self, environment):
                return await self.exec_as_agent(
                    environment,
                    "curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh "
                    "| bash -s -- 2.1.90",
                )

            async def run(self, instruction, environment, context):
                return None

            async def exec_as_root(
                self, environment, command=None, env=None, cwd=None, timeout_sec=None
            ):
                captured.append(command)
                return command

            async def exec_as_agent(
                self, environment, command=None, env=None, cwd=None, timeout_sec=None
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
            agent._extra_env = extra_env
            asyncio.run(agent.install(object()))

        return captured

    def _install_command(self, extra_env: dict[str, str]) -> str:
        captured = self._install_commands(extra_env)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_node_dist_url_bootstrap_included_when_configured(self) -> None:
        url = "https://registry.npmmirror.com/-/binary/node/v22.14.0/node-v22.14.0-linux-x64.tar.gz"
        command = self._install_command(
            {"CC_OPIK_ENABLE_HOOK": "false", "CC_NODE_DIST_URL": url}
        )
        self.assertIn(url, command)
        self.assertIn("@anthropic-ai/claude-code@2.1.90", command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_node_dist_url_bootstrap_skipped_when_unset(self) -> None:
        command = self._install_command({"CC_OPIK_ENABLE_HOOK": "false"})
        self.assertIn("node_dist_url=''", command)
        self.assertIn('[ -n "$node_dist_url" ]', command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_node_dist_extraction_failure_can_reach_package_manager_fallback(self) -> None:
        command = self._install_command(
            {
                "CC_OPIK_ENABLE_HOOK": "false",
                "CC_NODE_DIST_URL": "https://example.com/node.tar.gz",
            }
        )

        guarded_extract = 'if extract_archive "$node_dist_tgz" "$node_dir"; then'
        package_manager_fallback = "if ! command -v npm >/dev/null 2>&1; then"
        self.assertIn(guarded_extract, command)
        self.assertLess(
            command.index(guarded_extract),
            command.index(package_manager_fallback, command.index(guarded_extract)),
        )

        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_hook_dependencies_use_rich_shared_pip_fallbacks(self) -> None:
        commands = self._install_commands(
            {
                "CC_OPIK_ENABLE_HOOK": "true",
                "CC_OPIK_INSTALL_DEPS": "true",
                "HARBOR_LOCAL_WHEEL_SERVER_URL": "https://cache.example/wheels",
                "OPIK_URL": "https://opik.example.invalid/api",
            }
        )
        command = next(item for item in commands if "mods = ('opik', 'uuid6', 'socksio')" in item)

        self.assertIn("export PIP_BREAK_SYSTEM_PACKAGES=1", command)
        self.assertIn("--retries 10 --timeout 120", command)
        self.assertIn("command -v curl", command)
        self.assertIn("command -v wget", command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_missing_python_skips_hook_dependencies_without_aborting_install(self) -> None:
        commands = self._install_commands(
            {
                "CC_OPIK_ENABLE_HOOK": "true",
                "CC_OPIK_INSTALL_DEPS": "true",
                "OPIK_URL": "https://opik.example.invalid/api",
            }
        )
        runtime_command = next(
            item for item in commands if "python_works()" in item
        )
        dependencies_command = next(
            item
            for item in commands
            if "mods = ('opik', 'uuid6', 'socksio')" in item
        )

        warning = "echo '[WARN] python missing, skip opik hook deps' >&2"
        self.assertIn(warning, runtime_command)
        self.assertIn(warning, dependencies_command)
        self.assertIn("exit 0", runtime_command[runtime_command.index(warning) :])
        self.assertIn(
            "exit 0", dependencies_command[dependencies_command.index(warning) :]
        )


class QzInstructionHookGateTest(unittest.TestCase):
    def run_hook(self, environment_type: str) -> mock.Mock:
        patch_instruction = mock.Mock()
        qz_task_instruction = types.ModuleType("qz_task_instruction")
        qz_task_instruction.patch_harbor_task_instruction = patch_instruction
        e2b_runtime = types.ModuleType("e2b_runtime")
        e2b_runtime.patch_e2b_runtime_from_env = mock.Mock()

        with (
            mock.patch.dict(
                os.environ,
                {
                    "HARBOR_ENVIRONMENT_TYPE": environment_type,
                    "QZ_SANDBOX_TEMPLATE_MAP": "/tmp/qz-map.json",
                },
                clear=True,
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "e2b_runtime": e2b_runtime,
                    "qz_task_instruction": qz_task_instruction,
                },
            ),
        ):
            load_module()
        return patch_instruction

    def test_instruction_patch_is_applied_for_qz(self) -> None:
        self.run_hook("qz").assert_called_once_with()

    def test_instruction_patch_is_not_applied_for_docker(self) -> None:
        self.run_hook("docker").assert_not_called()


if __name__ == "__main__":
    unittest.main()
