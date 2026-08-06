"""Execute configured run-local Harbor controller actions."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ControllerResult:
    """One controller action result returned to the monitor loop."""

    action: dict[str, Any]
    history: list[dict[str, Any]]
    control_stdout: str | None = None


def build_control_argv(
    control_cmd: str,
    run_dir: Path,
    label: str,
) -> tuple[list[str] | None, str | None]:
    try:
        parts = shlex.split(control_cmd)
    except ValueError as exc:
        return None, f"{label}_cmd_parse_error={exc}"
    if not parts:
        return None, f"{label}_cmd_empty"
    if parts[0] in {"bash", "sh", "python", "python3"}:
        return None, f"{label}_cmd_interpreter_prefixed"

    run_root = run_dir.resolve()
    executable = Path(parts[0])
    if not executable.is_absolute():
        executable = run_root / executable
    try:
        resolved = executable.resolve()
        resolved.relative_to(run_root)
    except (OSError, ValueError):
        return None, f"{label}_cmd_not_run_specific"
    if not resolved.exists():
        return None, f"{label}_cmd_missing"
    if not resolved.is_file():
        return None, f"{label}_cmd_not_file"
    if not os.access(resolved, os.X_OK):
        return None, f"{label}_cmd_not_executable"
    return [str(resolved), *parts[1:]], None


def _notify_action(
    *,
    state: dict[str, Any],
    reason: str,
    control_type: str,
    attempted: bool,
    performed: bool,
    exit_code: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "notify",
        "retry_count": state.get("retry_count", 0),
        "reason": reason,
        "control_type": control_type,
        "control_attempted": attempted,
        "external_control_performed": performed,
    }
    if exit_code is not None:
        action["control_exit_code"] = exit_code
    if error is not None:
        action["control_error"] = error
    return action


def _run_command(argv: list[str], run_dir: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=run_dir,
        shell=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )


def _execute_restart(
    requested_action: dict[str, Any],
    *,
    restart_cmd: str | None,
    run_dir: Path,
    state: dict[str, Any],
    observation: dict[str, Any],
    history: list[dict[str, Any]],
    timeout_seconds: int,
) -> ControllerResult:
    if not restart_cmd:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="restart_needed_but_restart_cmd_missing",
                control_type="restart",
                attempted=False,
                performed=False,
            ),
            history=history,
        )

    argv, control_error = build_control_argv(restart_cmd, run_dir, "restart")
    if control_error or argv is None:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason=control_error or "restart_cmd_invalid",
                control_type="restart",
                attempted=False,
                performed=False,
            ),
            history=history,
        )

    state["retry_count"] = state.get("retry_count", 0) + 1
    try:
        result = _run_command(argv, run_dir, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="restart_failed_timeout",
                control_type="restart",
                attempted=True,
                performed=True,
                error=str(exc),
            ),
            history=history,
        )
    except Exception as exc:  # noqa: BLE001 - control boundary reports failures
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="restart_failed_exception",
                control_type="restart",
                attempted=True,
                performed=False,
                error=str(exc),
            ),
            history=history,
        )

    if result.returncode != 0:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason=f"restart_failed_exit_code={result.returncode}",
                control_type="restart",
                attempted=True,
                performed=True,
                exit_code=result.returncode,
            ),
            history=history,
            control_stdout=result.stdout[-2000:],
        )

    control_ts = time.time()
    state["last_progress_ts"] = control_ts
    state["run_start_ts"] = control_ts
    restart_history = [
        {
            "ts": control_ts,
            "finished": observation["finished"],
            "running": observation["running"],
            "remaining": observation.get("unclaimed_remaining"),
            "unfinished": observation.get("unfinished"),
            "status": "restart_executed",
        }
    ]
    action = {
        "type": "restart",
        "retry_count": state["retry_count"],
        "reason": requested_action["reason"],
        "control_type": "restart",
        "control_exit_code": result.returncode,
        "control_attempted": True,
        "external_control_performed": True,
    }
    return ControllerResult(
        action=action,
        history=restart_history,
        control_stdout=result.stdout[-2000:],
    )


def _execute_stop(
    requested_action: dict[str, Any],
    *,
    stop_cmd: str | None,
    run_dir: Path,
    state: dict[str, Any],
    history: list[dict[str, Any]],
    timeout_seconds: int,
) -> ControllerResult:
    if not stop_cmd:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="stop_needed_but_stop_cmd_missing",
                control_type="stop",
                attempted=False,
                performed=False,
            ),
            history=history,
        )

    argv, control_error = build_control_argv(stop_cmd, run_dir, "stop")
    if control_error or argv is None:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason=control_error or "stop_cmd_invalid",
                control_type="stop",
                attempted=False,
                performed=False,
            ),
            history=history,
        )

    try:
        result = _run_command(argv, run_dir, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="stop_failed_timeout",
                control_type="stop",
                attempted=True,
                performed=True,
                error=str(exc),
            ),
            history=history,
        )
    except Exception as exc:  # noqa: BLE001 - control boundary reports failures
        return ControllerResult(
            action=_notify_action(
                state=state,
                reason="stop_failed_exception",
                control_type="stop",
                attempted=True,
                performed=False,
                error=str(exc),
            ),
            history=history,
        )

    action = {
        "type": "stop" if result.returncode == 0 else "notify",
        "retry_count": state.get("retry_count", 0),
        "reason": (
            requested_action["reason"]
            if result.returncode == 0
            else f"stop_failed_exit_code={result.returncode}"
        ),
        "control_type": "stop",
        "control_exit_code": result.returncode,
        "control_attempted": True,
        "external_control_performed": True,
    }
    return ControllerResult(
        action=action,
        history=history,
        control_stdout=result.stdout[-2000:],
    )


def execute_action(
    requested_action: dict[str, Any],
    *,
    restart_cmd: str | None,
    stop_cmd: str | None,
    run_dir: Path,
    state: dict[str, Any],
    observation: dict[str, Any],
    history: list[dict[str, Any]],
    timeout_seconds: int = 120,
) -> ControllerResult:
    """Execute a selected controller action with existing compatibility semantics."""

    action_type = requested_action.get("type")
    if action_type == "restart":
        return _execute_restart(
            requested_action,
            restart_cmd=restart_cmd,
            run_dir=run_dir,
            state=state,
            observation=observation,
            history=history,
            timeout_seconds=timeout_seconds,
        )
    if action_type == "stop":
        return _execute_stop(
            requested_action,
            stop_cmd=stop_cmd,
            run_dir=run_dir,
            state=state,
            history=history,
            timeout_seconds=timeout_seconds,
        )
    return ControllerResult(action=requested_action, history=history)
