from __future__ import annotations

import base64
import hashlib
import importlib.util
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
import types
import unittest
from unittest import mock


RUNTIME_PATCH = Path(__file__).resolve().parents[1] / "sitecustomize.py"


def load_runtime_patch():
    spec = importlib.util.spec_from_file_location(
        "test_harbor_claude_sitecustomize", RUNTIME_PATCH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load runtime patch from {RUNTIME_PATCH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PATCH = load_runtime_patch()


class FakeAgent:
    def __init__(self, identity: str = "1000 1001\n"):
        self.identity = identity
        self.agent_commands: list[str] = []
        self.root_commands: list[str] = []

    async def exec_as_agent(
        self, environment, command, env=None, cwd=None, timeout_sec=None
    ):
        self.agent_commands.append(command)
        if "id -u" in command and "id -g" in command:
            return SimpleNamespace(stdout=self.identity, return_code=0)
        return SimpleNamespace(stdout="", return_code=0)

    async def exec_as_root(
        self, environment, command, env=None, cwd=None, timeout_sec=None
    ):
        self.root_commands.append(command)
        return SimpleNamespace(stdout="", return_code=0)


class FakeEnvironment:
    def __init__(self):
        self.uploads: list[tuple[Path, str, bytes]] = []
        self.commands: list[dict[str, object]] = []

    async def upload_file(self, source_path, target_path):
        source = Path(source_path)
        self.uploads.append((source, target_path, source.read_bytes()))

    async def exec(
        self, command, cwd=None, env=None, timeout_sec=None, user=None
    ):
        self.commands.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "timeout_sec": timeout_sec,
                "user": user,
            }
        )
        return SimpleNamespace(stdout="", return_code=0)


class FakeLogger:
    def __init__(self):
        self.warnings: list[tuple[object, ...]] = []

    def warning(self, *args):
        self.warnings.append(args)

    def debug(self, *args, **kwargs):
        return None


def load_runtime_patch_with_fake_harbor():
    class FakeClaudeCode:
        def __init__(self, prompt: str):
            self._resolved_flags = {"append_system_prompt": prompt}
            self._extra_env = {}
            self.logger = FakeLogger()

        async def install(self, environment):
            return None

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

        async def run(self, instruction, environment, context):
            command = "claude --verbose --output-format=stream-json"
            prompt = self._resolved_flags.get("append_system_prompt")
            if prompt is not None:
                command += f" --append-system-prompt {prompt}"
            command += (
                " --print -- old-task 2>&1 </dev/null | tee "
                "/logs/agent/claude-code.txt"
            )
            return await self.exec_as_agent(environment, command)

        def populate_context_post_run(self, context):
            return None

        def _convert_events_to_trajectory(self, session_dir):
            return None

    module_names = (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.agents.installed.claude_code",
    )
    modules = {name: types.ModuleType(name) for name in module_names}
    modules["harbor.agents.installed.claude_code"].ClaudeCode = FakeClaudeCode
    with mock.patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "test_harbor_claude_sitecustomize_integration", RUNTIME_PATCH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load runtime patch from {RUNTIME_PATCH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module, FakeClaudeCode


class PromptTransportUnitTests(unittest.TestCase):
    def test_local_file_preserves_adversarial_prompt_bytes(self):
        prompt = (
            "--depth --inplace --index-url --csv_path\n"
            "single ' double \" backtick ` command $(touch /tmp/bad); 中文\n"
        )
        path, size, digest = PATCH._write_local_append_system_prompt(prompt)
        try:
            payload = prompt.encode("utf-8")
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(size, len(payload))
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            path.unlink(missing_ok=True)

    def test_file_flag_is_inserted_before_first_print_delimiter(self):
        remote = "/tmp/prompt path/with'quote.txt"
        command = (
            "claude --verbose --output-format=stream-json "
            "--permission-mode=bypassPermissions --print -- "
            "'task mentions --depth and --csv_path' 2>&1"
        )
        patched = PATCH._inject_append_system_prompt_file(command, remote)
        prefix, marker, instruction = patched.partition(" --print --")

        self.assertEqual(marker, " --print --")
        self.assertEqual(instruction, " 'task mentions --depth and --csv_path' 2>&1")
        argv = shlex.split(prefix)
        flag_index = argv.index("--append-system-prompt-file")
        self.assertEqual(argv[flag_index + 1], remote)

    def test_inline_duplicate_and_missing_insertion_points_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Unsafe inline"):
            PATCH._inject_append_system_prompt_file(
                "claude --append-system-prompt unsafe --print -- task",
                "/tmp/prompt.txt",
            )
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            PATCH._inject_append_system_prompt_file(
                "claude --append-system-prompt-file /tmp/old --print -- task",
                "/tmp/new",
            )
        with self.assertRaisesRegex(RuntimeError, "missing the --print"):
            PATCH._inject_append_system_prompt_file(
                "claude --verbose", "/tmp/prompt.txt"
            )

    def test_main_instruction_and_exit_status_are_hardened(self):
        instruction = "--csv_path `unterminated 'quote $(touch /tmp/bad); 中文"
        command = (
            "claude --verbose --output-format=stream-json --print -- old-task "
            "2>&1 </dev/null | tee /logs/agent/claude-code.txt"
        )
        patched = PATCH._replace_print_instruction(command, instruction)
        encoded = base64.b64encode(instruction.encode("utf-8")).decode("ascii")

        self.assertNotIn(instruction, patched)
        self.assertIn(encoded, patched)
        self.assertIn("base64 -d", patched)
        diagnosed = PATCH._append_claude_exit_diagnostics(patched)
        self.assertIn("PIPESTATUS", diagnosed)
        self.assertIn("claude-wrapper-exit.log", diagnosed)
        self.assertEqual(PATCH._append_claude_exit_diagnostics(diagnosed), diagnosed)

    def test_runtime_removes_inline_prompt_flag_before_harbor_builds_command(self):
        source = RUNTIME_PATCH.read_text(encoding="utf-8")
        self.assertIn('run_flags.pop("append_system_prompt", _MISSING)', source)
        self.assertNotIn("def _fix_unquoted_append_system_prompt", source)


class PromptTransportRemoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_patched_run_never_embeds_prompt_in_shell_command(self):
        _module, claude_code = load_runtime_patch_with_fake_harbor()
        prompt = (
            "PROMPT_RAW_SENTINEL --depth --csv_path `unterminated 'quote\n"
            "$(touch /tmp/prompt-transport-must-not-run); 中文"
        )
        environment = FakeEnvironment()
        agent = claude_code(prompt)
        saved_flags = agent._resolved_flags

        await agent.run("ordinary instruction", environment, SimpleNamespace())

        self.assertIs(agent._resolved_flags, saved_flags)
        self.assertEqual(agent._resolved_flags["append_system_prompt"], prompt)
        self.assertEqual(len(environment.uploads), 1)
        local_path, remote_path, uploaded = environment.uploads[0]
        self.assertEqual(uploaded, prompt.encode("utf-8"))
        self.assertFalse(local_path.exists())

        all_commands = "\n".join(
            str(item["command"]) for item in environment.commands
        )
        self.assertNotIn("PROMPT_RAW_SENTINEL", all_commands)
        claude_commands = [
            str(item["command"])
            for item in environment.commands
            if "claude --verbose --output-format=stream-json"
            in str(item["command"])
        ]
        self.assertEqual(len(claude_commands), 1)
        self.assertIn("--append-system-prompt-file", claude_commands[0])
        self.assertIn(shlex.quote(remote_path), claude_commands[0])
        self.assertNotIn(" --append-system-prompt ", claude_commands[0])
        self.assertTrue(
            any(
                item["user"] == "root"
                and f"rm -f -- {shlex.quote(remote_path)}" in str(item["command"])
                for item in environment.commands
            )
        )

    async def test_remote_prompt_is_owned_readable_and_removed(self):
        prompt = "file-backed prompt\n"
        local_path, _size, _digest = PATCH._write_local_append_system_prompt(prompt)
        agent = FakeAgent()
        environment = FakeEnvironment()
        logger = FakeLogger()
        remote_path = "/tmp/prompt path/prompt.txt"
        try:
            await PATCH._install_remote_append_system_prompt(
                agent, environment, local_path, remote_path
            )
            self.assertEqual(environment.uploads[0][2], prompt.encode("utf-8"))
            quoted = shlex.quote(remote_path)
            self.assertIn(f"chown 1000:1001 {quoted}", agent.root_commands[0])
            self.assertIn(f"chmod 600 {quoted}", agent.root_commands[0])
            self.assertIn(f"test -f {quoted} && test -r {quoted}", agent.agent_commands[-1])

            await PATCH._remove_remote_append_system_prompt(
                environment, remote_path, logger
            )
            self.assertEqual(environment.commands[-1]["user"], "root")
            self.assertIn(f"rm -f -- {quoted}", environment.commands[-1]["command"])
            self.assertEqual(logger.warnings, [])
        finally:
            local_path.unlink(missing_ok=True)

    async def test_invalid_runtime_identity_fails_before_upload(self):
        local_path, _size, _digest = PATCH._write_local_append_system_prompt("x")
        agent = FakeAgent(identity="invalid\n")
        environment = FakeEnvironment()
        try:
            with self.assertRaisesRegex(RuntimeError, "UID/GID"):
                await PATCH._install_remote_append_system_prompt(
                    agent, environment, local_path, "/tmp/prompt.txt"
                )
            self.assertEqual(environment.uploads, [])
        finally:
            local_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
