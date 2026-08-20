"""Model-fusion-only overlay for Harbor's Claude Code integration.

The shared Harbor launcher selects this directory through
``HARBOR_CLAUDE_CODE_DIR`` only from ``run_one_tb21_task.sh``. This module
loads the repository's normal Claude/Opik patch first, then layers the
mid-turn gate, subagent definitions, and file-backed prompt transport on top.
Ordinary Harbor runs never import this module.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import importlib.util
import json
import os
import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from types import MethodType, ModuleType

_MISSING = object()
_ROUND_GATE_EVENTS = ("PreToolUse", "Stop")
_ACTIVE_EXTRA_ENV: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("model_fusion_extra_env", default=None)
)


def _load_base_sitecustomize() -> ModuleType:
    base_path = (
        Path(__file__).resolve().parents[4]
        / "Harbor-claude-code"
        / "sitecustomize.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_agent_fleet_base_sitecustomize", base_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load base sitecustomize: {base_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_base_sitecustomize()
_BASE_BUILD_HOOK_SETTINGS_JSON = _BASE._build_hook_settings_json


def _is_true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _extra_value(
    extra_env: dict[str, str] | None, name: str, default: str = ""
) -> str:
    return (
        (extra_env or {}).get(name, "")
        or os.environ.get(name, "")
        or default
    )


def _round_gate_enabled(extra_env: dict[str, str] | None) -> bool:
    return _is_true(_extra_value(extra_env, "HARBOR_FUSION_ROUND_GATE"))


def _compose_hook_settings(
    hook_path: str,
    extra_env: dict[str, str] | None,
    *,
    opik_enabled: bool,
) -> str:
    if opik_enabled:
        payload = json.loads(_BASE_BUILD_HOOK_SETTINGS_JSON(hook_path))
    else:
        payload = {"alwaysThinkingEnabled": True, "hooks": {}}

    if not _round_gate_enabled(extra_env):
        return json.dumps(payload, ensure_ascii=True)

    gate_path = _extra_value(
        extra_env,
        "HARBOR_FUSION_ROUND_GATE_PATH",
        "/opt/tb-fusion-round/subagent_barrier_gate.py",
    )
    gate_mode = _extra_value(
        extra_env,
        "HARBOR_FUSION_ROUND_GATE_MODE",
        _extra_value(extra_env, "SPAN_FORCE_MODE", "mid-turn-fusion"),
    )
    hooks = payload.setdefault("hooks", {})
    for event in _ROUND_GATE_EVENTS:
        hooks.setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            f"python3 {shlex.quote(gate_path)} "
                            f"{shlex.quote(event)} --mode {shlex.quote(gate_mode)}"
                        ),
                    }
                ]
            }
        )
    return json.dumps(payload, ensure_ascii=True)


def _base_hook_settings_with_fusion(hook_path: str) -> str:
    return _compose_hook_settings(
        hook_path,
        _ACTIVE_EXTRA_ENV.get(),
        opik_enabled=True,
    )


# The base patch resolves this global when ClaudeCode.run executes. Replacing
# it here changes only this Python process, whose PYTHONPATH was selected by
# the model-fusion launcher.
_BASE._build_hook_settings_json = _base_hook_settings_with_fusion


def _write_local_prompt(prompt: str) -> tuple[Path, int, str]:
    payload = prompt.encode("utf-8")
    fd, raw_path = tempfile.mkstemp(
        prefix="harbor-claude-append-system-prompt-", suffix=".txt"
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path, len(payload), hashlib.sha256(payload).hexdigest()


async def _install_remote_prompt(
    agent, environment, local_path: Path, remote_path: str
) -> None:
    identity = await agent.exec_as_agent(
        environment,
        command='printf "%s %s\\n" "$(id -u)" "$(id -g)"',
        timeout_sec=30,
    )
    identity_parts = (identity.stdout or "").strip().split()
    if len(identity_parts) != 2 or not all(part.isdigit() for part in identity_parts):
        raise RuntimeError(
            "unable to resolve Claude runtime UID/GID for append-system-prompt-file"
        )
    uid, gid = identity_parts

    await environment.upload_file(local_path, remote_path)
    quoted_path = shlex.quote(remote_path)
    await agent.exec_as_root(
        environment,
        command=f"chown {uid}:{gid} {quoted_path} && chmod 600 {quoted_path}",
        timeout_sec=30,
    )
    await agent.exec_as_agent(
        environment,
        command=f"test -f {quoted_path} && test -r {quoted_path}",
        timeout_sec=30,
    )


async def _remove_remote_prompt(environment, remote_path: str, logger) -> None:
    try:
        result = await environment.exec(
            command=f"rm -f -- {shlex.quote(remote_path)}",
            user="root",
            timeout_sec=30,
        )
        if result.return_code != 0:
            logger.warning(
                "Failed to remove remote append-system-prompt file %s (exit %s)",
                remote_path,
                result.return_code,
            )
    except Exception as exc:  # noqa: BLE001 - remote cleanup is best effort
        logger.warning(
            "Failed to remove remote append-system-prompt file %s: %s",
            remote_path,
            exc,
        )


def _claude_stream_command(command: str) -> bool:
    return (
        "claude --verbose --output-format=stream-json" in command
        and " --print --" in command
    )


def _inject_prompt_file(command: str, remote_path: str) -> str:
    marker = " --print --"
    marker_pos = command.find(marker)
    if marker_pos < 0:
        raise RuntimeError("Claude command is missing the --print -- insertion point")
    if " --append-system-prompt " in command:
        raise RuntimeError("unsafe inline --append-system-prompt remained")
    if " --append-system-prompt-file " in command:
        raise RuntimeError("duplicate --append-system-prompt-file")
    addition = f" --append-system-prompt-file {shlex.quote(remote_path)}"
    return command[:marker_pos] + addition + command[marker_pos:]


def _replace_instruction(command: str, instruction: str) -> str:
    marker = " --print -- "
    tail_marker = " 2>&1 </dev/null | tee "
    start = command.find(marker)
    if start < 0:
        return command
    value_start = start + len(marker)
    end = command.find(tail_marker, value_start)
    if end < 0:
        return command
    encoded = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
    safe_value = f'"$(printf %s {shlex.quote(encoded)} | base64 -d)"'
    return command[:value_start] + safe_value + command[end:]


def _inject_agents(command: str, extra_env: dict[str, str] | None) -> str:
    agents_json = _extra_value(extra_env, "HARBOR_CLAUDE_CODE_AGENTS_JSON").strip()
    prefix = "claude --verbose --output-format=stream-json"
    if not agents_json or " --agents " in command or prefix not in command:
        return command
    marker = " --print --"
    addition = f" --agents {shlex.quote(agents_json)}"
    if marker in command:
        return command.replace(marker, addition + marker, 1)
    return command.replace(prefix, prefix + addition, 1)


def _append_exit_diagnostics(command: str) -> str:
    marker = " 2>&1 </dev/null | tee "
    if marker not in command or "claude-wrapper-exit.log" in command:
        return command
    return command + (
        '; __tb_pipeline_status=("${PIPESTATUS[@]}"); '
        "__tb_pipeline_rc=0; "
        'for __tb_status in "${__tb_pipeline_status[@]}"; do '
        'if [ "$__tb_status" -ne 0 ]; then __tb_pipeline_rc="$__tb_status"; fi; '
        "done; "
        'printf "%s\\n" '
        '"time=$(date -Is) pipeline=${__tb_pipeline_status[*]} return=$__tb_pipeline_rc" '
        ">> /logs/agent/claude-wrapper-exit.log 2>&1 || true; "
        'exit "$__tb_pipeline_rc"'
    )


def _patch_model_fusion_run() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:  # noqa: BLE001 - Harbor is optional at interpreter startup
        return

    if getattr(ClaudeCode, "_model_fusion_patch_applied", False):
        return

    original_run = ClaudeCode.run

    async def patched_run(self, instruction, environment, context):
        extra_env = getattr(self, "_extra_env", None)
        saved_flags = getattr(self, "_resolved_flags", None)
        if not isinstance(saved_flags, dict):
            raise TypeError(
                "model-fusion requires Harbor ClaudeCode._resolved_flags"
            )

        run_flags = dict(saved_flags)
        append_prompt = run_flags.pop("append_system_prompt", _MISSING)
        prompt_configured = append_prompt is not _MISSING
        gate_enabled = _round_gate_enabled(extra_env)
        agents_enabled = bool(
            _extra_value(extra_env, "HARBOR_CLAUDE_CODE_AGENTS_JSON").strip()
        )
        if not prompt_configured and not gate_enabled and not agents_enabled:
            return await original_run(self, instruction, environment, context)

        original_exec_as_agent = self.exec_as_agent
        local_prompt_path: Path | None = None
        remote_prompt_path = ""

        async def exec_as_agent_with_fusion(
            _self,
            environment,
            command,
            env=None,
            cwd=None,
            timeout_sec=None,
        ):
            patched_command = command
            if _claude_stream_command(patched_command):
                if remote_prompt_path:
                    patched_command = _inject_prompt_file(
                        patched_command, remote_prompt_path
                    )
                patched_command = _replace_instruction(patched_command, instruction)
                patched_command = _inject_agents(patched_command, extra_env)

            # With Opik disabled, the base patch does not write settings. Add
            # the gate-only payload here; with Opik enabled, the composed base
            # builder has already written both hook groups and set this guard.
            if (
                gate_enabled
                and "CLAUDE_CONFIG_DIR/debug" in command
                and "mkdir -p" in command
                and not getattr(_self, "_opik_hook_settings_written", False)
            ):
                settings_json = shlex.quote(
                    _compose_hook_settings(
                        _BASE._hook_mount_path(extra_env),
                        extra_env,
                        opik_enabled=False,
                    )
                )
                patched_command = (
                    f"{patched_command} && "
                    "mkdir -p $HOME/.claude && "
                    f"printf '%s\\n' {settings_json} > $CLAUDE_CONFIG_DIR/settings.json && "
                    f"printf '%s\\n' {settings_json} > $HOME/.claude/settings.json"
                )
                _self._opik_hook_settings_written = True

            if _claude_stream_command(patched_command):
                patched_command = _append_exit_diagnostics(patched_command)

            return await original_exec_as_agent(
                environment,
                patched_command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        self._resolved_flags = run_flags
        context_token = _ACTIVE_EXTRA_ENV.set(extra_env)
        try:
            if prompt_configured:
                local_prompt_path, prompt_size, prompt_sha256 = _write_local_prompt(
                    str(append_prompt)
                )
                remote_prompt_path = (
                    "/tmp/.harbor-claude-append-system-prompt-"
                    f"{uuid.uuid4().hex}.txt"
                )
                await _install_remote_prompt(
                    self, environment, local_prompt_path, remote_prompt_path
                )
                self.logger.debug(
                    "Staged model-fusion append system prompt via file",
                    extra={
                        "prompt_size_bytes": prompt_size,
                        "prompt_sha256": prompt_sha256,
                        "remote_path": remote_prompt_path,
                    },
                )

            self.exec_as_agent = MethodType(exec_as_agent_with_fusion, self)
            try:
                return await original_run(self, instruction, environment, context)
            finally:
                self.exec_as_agent = original_exec_as_agent
        finally:
            _ACTIVE_EXTRA_ENV.reset(context_token)
            self._resolved_flags = saved_flags
            if remote_prompt_path:
                await _remove_remote_prompt(
                    environment, remote_prompt_path, self.logger
                )
            if local_prompt_path is not None:
                try:
                    local_prompt_path.unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "Failed to remove local append-system-prompt file %s: %s",
                        local_prompt_path,
                        exc,
                    )

    ClaudeCode.run = patched_run
    ClaudeCode._model_fusion_patch_applied = True


_patch_model_fusion_run()
