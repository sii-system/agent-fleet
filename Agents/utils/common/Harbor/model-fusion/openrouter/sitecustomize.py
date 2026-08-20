"""OpenRouter-fusion-only overlay for Harbor's Claude Code integration."""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from pathlib import Path
from types import MethodType, ModuleType


def _load_base_sitecustomize() -> ModuleType:
    base_path = Path(__file__).resolve().parents[5] / "Harbor-claude-code" / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location("_agent_fleet_openrouter_base", base_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load base sitecustomize: {base_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_base_sitecustomize()


def _value(extra_env: dict[str, str] | None, name: str, default: str = "") -> str:
    return (extra_env or {}).get(name, "") or os.environ.get(name, "") or default


def _wrap_claude_command(
    command: str,
    instruction: str,
    extra_env: dict[str, str] | None,
    workspace: str | os.PathLike[str] | None = None,
) -> str:
    if not extra_env or extra_env.get("OPENROUTER_ENABLED") != "1":
        return command
    marker = "claude --verbose --output-format=stream-json"
    redirect = " 2>&1 </dev/null | tee "
    marker_count = command.count(marker)
    if marker_count == 0:
        if "stream-json" in command or " --print --" in command:
            raise RuntimeError(
                "OpenRouter is enabled but the Claude command shape is unsupported"
            )
        return command
    if marker_count != 1 or command.count(redirect) != 1:
        raise RuntimeError(
            "OpenRouter is enabled but the Claude command shape is unsupported"
        )
    start = command.find(marker)
    end = command.find(redirect, start)
    if end <= start:
        raise RuntimeError(
            "OpenRouter is enabled but the Claude redirect precedes its command"
        )

    wheel = _value(extra_env, "OPENROUTER_WHEEL_PATH", "/opt/sii-fusion-router/router.whl")
    config = _value(extra_env, "OPENROUTER_CONFIG_PATH", "/opt/sii-fusion-router/router-config.json")
    version = _value(extra_env, "OPENROUTER_VERSION")
    task_id = _value(extra_env, "HARBOR_TASK_ID", "harbor-task")
    artifacts = _value(extra_env, "OPENROUTER_ARTIFACT_ROOT", "/logs/agent/router")
    summary = _value(extra_env, "OPENROUTER_SUMMARY_PATH", "/logs/agent/router-run-summary.json")
    runtime = "/tmp/sii-fusion-router-runtime"
    task_file = "/logs/agent/router-task.md"
    workspace_arg = (
        shlex.quote(os.fspath(workspace)) if workspace else '"$(pwd -P)"'
    )
    command_prefix = command[:start]
    original_claude = command[start:end]
    bootstrap = (
        f"rm -rf -- {shlex.quote(runtime)} && "
        f"mkdir -p {shlex.quote(runtime)} {shlex.quote(artifacts)} && "
        "python3 -c "
        + shlex.quote("import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])")
        + f" {shlex.quote(wheel)} {shlex.quote(runtime)} && "
        f"actual=$(PYTHONPATH={shlex.quote(runtime)} python3 -m sii_fusion_router.cli --version) && "
        f"test \"$actual\" = {shlex.quote(version)} && "
        f"printf '%s\\n' {shlex.quote(instruction)} > {shlex.quote(task_file)} && "
    )
    router = (
        f"PYTHONPATH={shlex.quote(runtime)} python3 -m sii_fusion_router.cli claude"
        " --pipeline openrouter_fusion"
        f" --task-id {shlex.quote(task_id)} --task-file {shlex.quote(task_file)}"
        f" --workspace {workspace_arg} --artifact-root {shlex.quote(artifacts)}"
        f" --summary {shlex.quote(summary)} --config {shlex.quote(config)}"
        " -- "
        f"{original_claude}"
    )
    return bootstrap + "{ " + command_prefix + router + "; }" + command[end:]


def _patch_run() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:  # noqa: BLE001 - Harbor is optional at interpreter startup
        return
    if getattr(ClaudeCode, "_openrouter_patch_applied", False):
        return
    original_run = ClaudeCode.run

    async def patched_run(self, instruction, environment, context):
        extra_env = getattr(self, "_extra_env", None)
        if not extra_env or extra_env.get("OPENROUTER_ENABLED") != "1":
            return await original_run(self, instruction, environment, context)
        original_exec = self.exec_as_agent

        async def wrapped(_self, environment, command, env=None, cwd=None, timeout_sec=None):
            return await original_exec(
                environment,
                _wrap_claude_command(command, instruction, extra_env, cwd),
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        self.exec_as_agent = MethodType(wrapped, self)
        try:
            return await original_run(self, instruction, environment, context)
        finally:
            self.exec_as_agent = original_exec

    ClaudeCode.run = patched_run
    ClaudeCode._openrouter_patch_applied = True


_patch_run()
