"""Mimo Max-only overlay for Harbor's Claude Code integration."""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from pathlib import Path
from types import MethodType, ModuleType


def _load_base_sitecustomize() -> ModuleType:
    base_path = (
        Path(__file__).resolve().parents[5]
        / "Harbor-claude-code"
        / "sitecustomize.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_agent_fleet_mimo_base_sitecustomize", base_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load base sitecustomize: {base_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_base_sitecustomize()


def _enabled(extra_env: dict[str, str] | None) -> bool:
    return bool(extra_env and extra_env.get("MIMO_ROUTER_ENABLED") == "1")


def _value(extra_env: dict[str, str] | None, name: str, default: str = "") -> str:
    return (extra_env or {}).get(name, "") or os.environ.get(name, "") or default


def _wrap_claude_command(
    command: str,
    instruction: str,
    extra_env: dict[str, str] | None,
    workspace: str | os.PathLike[str] | None = None,
) -> str:
    if not _enabled(extra_env):
        return command
    marker = "claude --verbose --output-format=stream-json"
    redirect = " 2>&1 </dev/null | tee "
    marker_count = command.count(marker)
    if marker_count == 0:
        if "stream-json" in command or " --print --" in command:
            raise RuntimeError(
                "Mimo Router is enabled but the Claude command shape is unsupported"
            )
        return command
    if marker_count != 1 or command.count(redirect) != 1:
        raise RuntimeError(
            "Mimo Router is enabled but the Claude command shape is unsupported"
        )
    start = command.find(marker)
    end = command.find(redirect, start)
    if end <= start:
        raise RuntimeError(
            "Mimo Router is enabled but the Claude redirect precedes its command"
        )

    wheel = _value(
        extra_env, "MIMO_ROUTER_WHEEL_PATH", "/opt/sii-fusion-router/router.whl"
    )
    config = _value(
        extra_env,
        "MIMO_ROUTER_CONFIG_PATH",
        "/opt/sii-fusion-router/router-config.json",
    )
    pipeline = _value(extra_env, "MIMO_ROUTER_PIPELINE", "mimo_max")
    version = _value(extra_env, "MIMO_ROUTER_VERSION")
    task_id = _value(extra_env, "HARBOR_TASK_ID", "harbor-task")
    artifact_root = _value(
        extra_env, "MIMO_ROUTER_ARTIFACT_ROOT", "/logs/agent/router"
    )
    summary = _value(
        extra_env,
        "MIMO_ROUTER_SUMMARY_PATH",
        "/logs/agent/router-run-summary.json",
    )
    runtime = "/tmp/sii-fusion-router-runtime"
    task_file = "/logs/agent/router-task.md"
    workspace_arg = (
        shlex.quote(os.fspath(workspace)) if workspace else '"$(pwd -P)"'
    )
    command_prefix = command[:start]
    original_claude = command[start:end]

    bootstrap = (
        f"rm -rf -- {shlex.quote(runtime)} && "
        f"mkdir -p {shlex.quote(runtime)} {shlex.quote(artifact_root)} && "
        "python3 -c "
        + shlex.quote(
            "import sys,zipfile; "
            "zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])"
        )
        + f" {shlex.quote(wheel)} {shlex.quote(runtime)} && "
        f"actual=$(PYTHONPATH={shlex.quote(runtime)} "
        "python3 -m sii_fusion_router.cli --version) && "
        f"test \"$actual\" = {shlex.quote(version)} && "
        f"printf '%s\\n' {shlex.quote(instruction)} > {shlex.quote(task_file)} && "
    )
    router = (
        f"PYTHONPATH={shlex.quote(runtime)} python3 -m sii_fusion_router.cli claude"
        f" --pipeline {shlex.quote(pipeline)}"
        f" --task-id {shlex.quote(task_id)}"
        f" --task-file {shlex.quote(task_file)}"
        f" --workspace {workspace_arg}"
        f" --artifact-root {shlex.quote(artifact_root)}"
        f" --summary {shlex.quote(summary)}"
        f" --config {shlex.quote(config)}"
        " -- "
        f"{original_claude}"
    )
    return bootstrap + "{ " + command_prefix + router + "; }" + command[end:]


def _patch_mimo_run() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:  # noqa: BLE001 - Harbor is optional at interpreter startup
        return
    if getattr(ClaudeCode, "_mimo_router_patch_applied", False):
        return

    original_run = ClaudeCode.run

    async def patched_run(self, instruction, environment, context):
        extra_env = getattr(self, "_extra_env", None)
        if not _enabled(extra_env):
            return await original_run(self, instruction, environment, context)
        original_exec_as_agent = self.exec_as_agent

        async def exec_as_agent_with_mimo(
            _self,
            environment,
            command,
            env=None,
            cwd=None,
            timeout_sec=None,
        ):
            return await original_exec_as_agent(
                environment,
                _wrap_claude_command(command, instruction, extra_env, cwd),
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        self.exec_as_agent = MethodType(exec_as_agent_with_mimo, self)
        try:
            return await original_run(self, instruction, environment, context)
        finally:
            self.exec_as_agent = original_exec_as_agent

    ClaudeCode.run = patched_run
    ClaudeCode._mimo_router_patch_applied = True


_patch_mimo_run()
