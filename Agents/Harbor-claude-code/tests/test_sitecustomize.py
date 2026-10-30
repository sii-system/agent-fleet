"""Regression tests for Claude Code command compatibility patches."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
    def test_opik_hook_receives_harbor_trial_identity(self) -> None:
        module = load_module()
        captured_env: list[dict[str, str] | None] = []

        class FakeClaudeCode:
            async def install(self, environment):
                return None

            async def run(self, instruction, environment, context):
                command = (
                    "claude --verbose --output-format=stream-json "
                    "--permission-mode=bypassPermissions --print -- 'task'"
                )
                return await self.exec_as_agent(environment, command)

            async def exec_as_agent(
                self, environment, command, env=None, cwd=None, timeout_sec=None
            ):
                captured_env.append(env)
                return command

        claude_code = types.ModuleType("harbor.agents.installed.claude_code")
        claude_code.ClaudeCode = FakeClaudeCode
        fake_modules = {
            name: types.ModuleType(name)
            for name in ("harbor", "harbor.agents", "harbor.agents.installed")
        }
        fake_modules["harbor.agents.installed.claude_code"] = claude_code

        with mock.patch.dict(sys.modules, fake_modules):
            module._patch_claude_code_realtime_hooks()
            agent = FakeClaudeCode()
            agent._extra_env = {
                "CC_OPIK_ENABLE_HOOK": "true",
                "OPIK_URL": "https://opik.example.invalid/api",
            }
            agent.session_id = "hello_sandbox__AbCdEfG__agent"
            asyncio.run(agent.run("task", object(), object()))

        self.assertEqual(
            captured_env,
            [{"HARBOR_TRIAL_ID": "hello_sandbox__AbCdEfG"}],
        )

    def test_opik_hooks_do_not_use_login_shells(self) -> None:
        module = load_module()
        settings = json.loads(
            module._build_hook_settings_json(
                "/opt/tb-opik/claude_realtime_trace.py"
            )
        )

        for event, entries in settings["hooks"].items():
            with self.subTest(event=event):
                command = entries[0]["hooks"][0]["command"]
                self.assertIn("sh -c ", command)
                self.assertNotIn("sh -lc ", command)

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

    def test_external_web_mcp_is_loaded_for_claude(self) -> None:
        module = load_module()
        captured: list[str] = []

        class FakeClaudeCode:
            async def install(self, environment):
                return None

            async def run(self, instruction, environment, context):
                return await self.exec_as_agent(
                    environment,
                    "claude --verbose --output-format=stream-json "
                    "--permission-mode=bypassPermissions --print -- 'task'",
                )

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
        fake_modules["harbor.agents.installed.claude_code"] = claude_code

        with mock.patch.dict(sys.modules, fake_modules):
            module._patch_claude_code_realtime_hooks()
            agent = FakeClaudeCode()
            agent._extra_env = {
                "CC_OPIK_ENABLE_HOOK": "false",
                "CC_WEB_MCP_PATH": "/opt/agent-fleet/exa_web_mcp.py",
            }
            asyncio.run(agent.run("task", object(), object()))

        self.assertEqual(len(captured), 1)
        self.assertIn('> "$HOME/.claude/web-mcp.json"', captured[0])
        self.assertIn('"command": "python3"', captured[0])
        self.assertIn('"args": ["/opt/agent-fleet/exa_web_mcp.py"]', captured[0])
        self.assertIn('"WEB_MCP_INSTRUCTION": "task"', captured[0])
        argv = shlex.split(captured[0].split("; ")[-1])
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "$HOME/.claude/web-mcp.json")
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1],
            "mcp__web__web_search,mcp__web__web_fetch",
        )


    def test_web_mcp_instruction_is_task_scoped(self):
        module = load_module()
        for question in ("Task one with 'quotes'\n\nand lines", "A different task"):
            configured = json.loads(module._build_web_mcp_config("/mcp.pyz", question))
            self.assertEqual(configured["mcpServers"]["web"]["env"], {
                "WEB_MCP_INSTRUCTION": question,
            })


class ClaudeInstallCommandTest(unittest.TestCase):
    def _install_command(
        self,
        extra_env: dict[str, str],
        *,
        environment_type: str = "docker",
    ) -> str:
        module = load_module()
        captured: list[str] = []
        captured_root: list[str] = []

        class FakeClaudeCode:
            async def install(self, environment):
                await self.exec_as_root(
                    environment,
                    "if command -v apk >/dev/null; then "
                    "apk add --no-cache curl bash nodejs npm procps; else "
                    "apt-get update && apt-get install -y curl procps; fi",
                )
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
                captured_root.append(command)
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

        with mock.patch.dict(
            os.environ, {"HARBOR_ENVIRONMENT_TYPE": environment_type}
        ), mock.patch.dict(sys.modules, fake_modules):
            module._patch_claude_code_realtime_hooks()
            agent = FakeClaudeCode()
            agent._extra_env = extra_env
            asyncio.run(agent.install(object()))

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured_root), 1)
        self.last_root_command = captured_root[0]
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
        self.assertIn("[ -n '' ]", command)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_s3_node_runtime_replaces_unsupported_system_node(self) -> None:
        command = self._install_command(
            {"CC_OPIK_ENABLE_HOOK": "false"},
            environment_type="opensandbox",
        )

        self.assertIn("node_runtime_required()", command)
        self.assertIn(
            'Number(process.versions.node.split(".")[0]) >= 18', command
        )
        self.assertIn(
            "! node -e 'process.exit(Number(process.versions.node.split(\".\")[0]) >= 18 ? 0 : 1)'",
            command,
        )
        self.assertIn(
            "if [ -f "
            "/opt/tb-opik/python-wheels/node-runtime.tar.gz ]",
            command,
        )
        self.assertIn(
            "tar -xzf /opt/tb-opik/python-wheels/node-runtime.tar.gz "
            '-C "$node_dir"',
            command,
        )
        self.assertEqual(
            command.count("find \"$node_dir\" -type f -path '*/bin/node'"), 2
        )
        self.assertNotIn("find \"$node_dir\" -path '*/bin/npm'", command)
        self.assertGreaterEqual(command.count("hash -r 2>/dev/null || true"), 3)
        self.assertIn("Using S3 Node runtime: $(node --version)", command)
        self.assertEqual(command.count("if node_runtime_required"), 2)

        bash_check = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_installs_pinned_claude_over_an_existing_image_executable(self) -> None:
        for existing in ("native", "symlink", "dangling-symlink", "absent"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "home"
                local_bin = home / ".local/bin"
                local_bin.mkdir(parents=True)
                claude = local_bin / "claude"
                old_content = "#!/bin/sh\necho old-image-version\n"
                target = root / "image-claude"
                target.write_text(old_content)
                target.chmod(0o755)
                if existing == "native":
                    claude.write_text(old_content)
                    claude.chmod(0o755)
                elif existing == "symlink":
                    claude.symlink_to(target)
                elif existing == "dangling-symlink":
                    claude.symlink_to(root / "missing-claude")
                sibling = local_bin / "unrelated-cli"
                sibling.write_text("keep this executable")
                binaries = root / "bin"
                binaries.mkdir()
                node = binaries / "node"
                node.write_text("#!/bin/sh\necho v22.0.0\n")
                node.chmod(0o755)
                npm = binaries / "npm"
                npm.write_text(
                    "#!/bin/bash\nset -euo pipefail\n"
                    '[[ "$1" == install ]] || exit 0\n'
                    'if [[ -e "$HOME/.local/bin/claude" || -L "$HOME/.local/bin/claude" ]]; then\n'
                    '  echo "npm error EEXIST" >&2; exit 73\nfi\n'
                    'printf "#!/bin/sh\\necho 2.1.90\\n" > "$HOME/.local/bin/claude"\n'
                    'chmod +x "$HOME/.local/bin/claude"\n'
                )
                npm.chmod(0o755)
                archive = root / "claude-code.tgz"
                archive.write_bytes(b"test fixture")
                command = self._install_command({
                    "CC_OPIK_ENABLE_HOOK": "false",
                    "CC_OPIK_CLAUDE_TGZ_PATH": str(archive),
                    "CC_OPIK_PY_WHEEL_DIR": str(root / "no-wheels"),
                })
                result = subprocess.run(
                    ["bash", "-c", command], capture_output=True, text=True, check=False,
                    env={**os.environ, "HOME": str(home), "TMPDIR": tmp,
                         "PATH": f"{binaries}:{os.environ['PATH']}"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("2.1.90", result.stdout)
                self.assertEqual(sibling.read_text(), "keep this executable")
                self.assertEqual(target.read_text(), old_content)
                backups = list(root.glob("harbor-claude-backup-*/claude"))
                self.assertEqual(len(backups), 0 if existing == "absent" else 1)
                if existing in ("native", "symlink"):
                    self.assertEqual(backups[0].read_text(), old_content)
                elif existing == "dangling-symlink":
                    self.assertTrue(backups[0].is_symlink())

    def test_s3_node_runtime_does_not_reenter_package_manager_fallback(self) -> None:
        self._assert_isolated_node_install(corrupt_cache=False)

    def test_docker_replaces_old_node_even_when_npm_is_present(self) -> None:
        self._assert_isolated_node_install(
            corrupt_cache=False, environment_type="docker", old_node=True
        )

    def test_s3_gzip_node_runtime_installs_without_python_or_xz(self) -> None:
        self._assert_isolated_node_install(corrupt_cache=False, minimal_path=True)

    def test_corrupt_node_cache_uses_configured_dist_before_failing(self) -> None:
        self._assert_isolated_node_install(corrupt_cache=True)

    def _assert_isolated_node_install(
        self, *, corrupt_cache: bool, minimal_path: bool = False,
        environment_type: str = "opensandbox", old_node: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_bin = root / "node-v22.23.2-linux-x64" / "bin"
            runtime_bin.mkdir(parents=True)
            node = runtime_bin / "node"
            node.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = --version ]; then echo v22.23.2; fi\n"
                "exit 0\n"
            )
            npm_script = (
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = --version ]; then echo 10.9.8; exit 0; fi\n"
                "if [ \"${1:-}\" = install ]; then\n"
                "  mkdir -p \"$HOME/.local/bin\"\n"
                "  printf '#!/bin/sh\\necho 2.1.90\\n' > \"$HOME/.local/bin/claude\"\n"
                "  chmod +x \"$HOME/.local/bin/claude\"\n"
                "fi\n"
                "exit 0\n"
            )
            npm = runtime_bin / "npm"
            npm.write_text(npm_script)
            npx = runtime_bin / "npx"
            npx.write_text(npm_script)
            for executable in (node, npm, npx):
                executable.chmod(0o755)

            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            node_archive = wheel_dir / "node-runtime.tar.gz"
            with tarfile.open(node_archive, "w:gz") as archive:
                archive.add(runtime_bin.parent, arcname=runtime_bin.parent.name)

            claude_tgz = root / "claude-code.tgz"
            claude_tgz.write_bytes(b"test fixture")
            extra_env = {
                "CC_OPIK_ENABLE_HOOK": "false",
                "CC_OPIK_CLAUDE_TGZ_PATH": str(claude_tgz),
                "CC_OPIK_PY_WHEEL_DIR": str(wheel_dir),
            }
            if corrupt_cache:
                dist = root / "node-dist.tar.xz"
                node_archive.rename(dist)
                node_archive.write_bytes(b"corrupt archive")
                extra_env["CC_NODE_DIST_URL"] = dist.as_uri()
            command = self._install_command(extra_env, environment_type=environment_type)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            apt_marker = root / "apt-called"
            fake_apt = fake_bin / "apt-get"
            fake_apt.write_text(
                f"#!/bin/sh\ntouch {shlex.quote(str(apt_marker))}\nexit 97\n"
            )
            fake_apt.chmod(0o755)
            if old_node:
                for name in ("node", "npm"):
                    binary = fake_bin / name
                    binary.write_text("#!/bin/sh\nexit 96\n")
                    binary.chmod(0o755)
            if minimal_path:
                for name in (
                    "chmod",
                    "cp",
                    "dirname",
                    "find",
                    "gzip",
                    "ln",
                    "mkdir",
                    "mktemp",
                    "mv",
                    "sleep",
                    "tar",
                ):
                    source = shutil.which(name)
                    self.assertIsNotNone(source)
                    assert source is not None
                    (fake_bin / name).symlink_to(source)
            if corrupt_cache:
                # Even a usable system Node must not bypass the isolated fallback.
                for name in ("node", "npm"):
                    binary = fake_bin / name
                    binary.write_text("#!/bin/sh\nexit 0\n")
                    binary.chmod(0o755)
            home = root / "home"
            home.mkdir()
            result = subprocess.run(
                ["/bin/bash", "-c", command],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": (
                        str(fake_bin)
                        if minimal_path
                        else f"{fake_bin}:{os.environ['PATH']}"
                    ),
                    "PYTHONWARNINGS": "error",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2.1.90", result.stdout)
            if not corrupt_cache:
                self.assertIn("Using S3 Node runtime: v22.23.2", result.stderr)
            else:
                self.assertEqual((fake_bin / "node").read_text(), "#!/bin/sh\nexit 0\n")
            self.assertFalse(apt_marker.exists(), result.stderr)

    def test_node_archive_filters_support_legacy_python(self) -> None:
        command = self._install_command({"CC_OPIK_ENABLE_HOOK": "false"})
        snippets = re.findall(r"import sys, tarfile\n.*?(?=\nPY\n)", command, re.DOTALL)
        self.assertEqual(len(snippets), 2)
        for snippet in snippets:
            self.assertEqual(snippet, load_module()._NODE_ARCHIVE_EXTRACT.rstrip())
            self.assertIn("tarfile.data_filter(member, path)", snippet)
            self.assertIn("tarfile.tar_filter(member, path)", snippet)
            self.assertNotIn("fully_trusted", snippet)

    def test_opensandbox_runtime_failures_do_not_install_system_node(self) -> None:
        for failure in ("corrupt-cache", "missing", "invalid-node", "unsafe-link", "corrupt-download"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "home"
                home.mkdir()
                wheels = root / "wheels"
                wheels.mkdir()
                binaries = root / "bin"
                binaries.mkdir()
                marker = root / "fallback-called"
                for name in ("apt-get", "apk", "yum", "tar"):
                    binary = binaries / name
                    binary.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 97\n")
                    binary.chmod(0o755)
                for name in ("node", "npm"):
                    binary = binaries / name
                    # A usable system Node must not hide a corrupt configured archive.
                    binary.write_text("#!/bin/sh\nexit " + ("0\n" if failure == "corrupt-cache" else "1\n"))
                    binary.chmod(0o755)
                system_node = (binaries / "node").read_bytes()
                node_archive = wheels / "node-runtime.tar.gz"
                extra_env = {
                    "CC_OPIK_ENABLE_HOOK": "false",
                    "CC_OPIK_PY_WHEEL_DIR": str(wheels),
                    "CC_OPIK_CLAUDE_TGZ_PATH": str(root / "claude.tgz"),
                }
                if failure == "corrupt-cache":
                    node_archive.write_bytes(b"not an archive")
                elif failure == "corrupt-download":
                    download = root / "node.tar.gz"
                    download.write_bytes(b"not an archive")
                    extra_env["CC_NODE_DIST_URL"] = download.as_uri()
                elif failure == "invalid-node":
                    with tarfile.open(node_archive, "w:gz") as archive:
                        archive.add(binaries / "node", arcname="node-runtime/bin/node")
                elif failure == "unsafe-link":
                    with tarfile.open(node_archive, "w:gz") as archive:
                        link = tarfile.TarInfo("node-runtime/bin/node")
                        link.type = tarfile.SYMTYPE
                        link.linkname = str(binaries / "node")
                        archive.addfile(link)
                command = self._install_command(extra_env, environment_type="opensandbox")
                result = subprocess.run(
                    ["bash", "-c", command], text=True, capture_output=True, timeout=15, check=False,
                    env={**os.environ, "HOME": str(home), "PYTHONWARNINGS": "error",
                         "PATH": f"{binaries}:{os.environ['PATH']}"},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to install system Node/npm", result.stderr)
                self.assertFalse(marker.exists(), result.stderr)
                self.assertFalse((home / ".local/bin/claude").exists())
                self.assertEqual((binaries / "node").read_bytes(), system_node)

    def test_non_opensandbox_keeps_node_package_fallback(self) -> None:
        command = self._install_command(
            {
                "CC_OPIK_ENABLE_HOOK": "false",
                "CC_NODE_DIST_URL": "https://example.com/node.tar.gz",
            },
            environment_type="docker",
        )

        guarded_extract = (
            'if python3 - <<\'PY\' "$node_dist_tgz" "$node_dir"'
        )
        package_manager_fallback = "if node_runtime_required; then"
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

    def test_opensandbox_preserves_non_node_prerequisites_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_tgz = root / "claude-code.tgz"
            wheel_dir = root / "wheels"
            self._install_command(
                {
                    "CC_OPIK_ENABLE_HOOK": "false",
                    "CC_OPIK_CLAUDE_TGZ_PATH": str(claude_tgz),
                    "CC_OPIK_PY_WHEEL_DIR": str(wheel_dir),
                },
                environment_type="opensandbox",
            )

            result = subprocess.run(
                ["bash", "-c", self.last_root_command],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotIn("nodejs npm", self.last_root_command)
        self.assertIn("command -v bash", self.last_root_command)
        self.assertIn("command -v ps", self.last_root_command)
        self.assertIn("command -v pgrep", self.last_root_command)
        self.assertIn("apt-get install -y bash procps", self.last_root_command)
        self.assertIn("apt-get update", self.last_root_command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("system prerequisites ready", result.stderr)
        bash_check = subprocess.run(
            ["bash", "-n"],
            input=self.last_root_command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

    def test_non_opensandbox_preserves_original_apt_bootstrap(self) -> None:
        self._install_command({"CC_OPIK_ENABLE_HOOK": "false"})

        self.assertNotIn("skip APT bootstrap", self.last_root_command)
        self.assertIn("apt-get update", self.last_root_command)


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
