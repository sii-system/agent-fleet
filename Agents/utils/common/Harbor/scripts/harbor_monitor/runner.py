"""Run the monitor loop and execute configured run-local controls."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .artifacts import (
    load_harbor_job_snapshot,
    load_manifest,
    load_state,
    load_task_file_manifest,
    read_first_existing_text,
    read_int,
    save_state,
    write_json,
)
from .contracts import (
    build_analyzer_handover,
    build_analyzer_handover_for_tasks,
    build_notify_incident_key,
    build_runner_action,
    build_user_notify,
)
from .evaluator import evaluate_once, now_ts


def build_control_argv(control_cmd: str, run_dir: Path, label: str) -> tuple[list[str] | None, str | None]:
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


def count_running_workers(queue_dir: Path) -> int:
    running = 0
    for current_file in queue_dir.glob("worker-*.current"):
        if not current_file.is_file():
            continue
        try:
            fields = current_file.read_text(encoding="utf-8").rstrip("\n").split("\t")
        except OSError:
            continue
        if len(fields) < 3:
            # Backward compatibility for existing Harbor runs.
            running += 1
            continue
        try:
            worker_pid = int(fields[-1])
            os.kill(worker_pid, 0)
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass
        running += 1
    return running


def process_is_alive(pid_file: Path | None) -> bool:
    if pid_file is None or not pid_file.is_file():
        return False
    try:
        fields = pid_file.read_text(encoding="utf-8").strip().split("\t")
        pid = int(fields[0])
        os.kill(pid, 0)
        if len(fields) > 1:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            if len(stat_fields) < 22 or stat_fields[21] != fields[1]:
                return False
    except (OSError, ValueError):
        return False
    return True


def read_exit_code(exit_file: Path | None) -> int | None:
    if exit_file is None or not exit_file.is_file():
        return None
    try:
        return int(exit_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run_loop(
    run_dir: Path,
    done_path: Path,
    failed_path: Path,
    queue_dir: Path | None,
    task_manifest_path: Path | None,
    task_file_path: Path | None,
    restart_cmd: str | None,
    stop_cmd: str | None,
    output_path: Path | None,
    poll_interval: int,
    max_retries: int,
    S_default: int,
    S_min: int,
    S_max: int,
    startup_grace: int,
    configured_timeout: int | None,
    total_override: int | None,
    running_override: int | None,
    claimed_override: int | None,
    remaining_override: int | None,
    user_report_output: Path | None,
    analyzer_handover_output: Path | None,
    runner_action_output: Path | None,
    loop_once: bool,
    include_unknown_not_complete: bool,
    harbor_job_dir_file: Path | None = None,
    harbor_pid_file: Path | None = None,
    harbor_exit_file: Path | None = None,
) -> None:
    state_path = run_dir / ".monitor_state.json"
    state = load_state(state_path)
    state.setdefault("retry_count", 0)
    state.setdefault("history", [])
    state.setdefault("consecutive_stall", 0)
    state.setdefault("last_progress_ts", None)
    state.setdefault("run_start_ts", None)
    state.setdefault("notify_recheck_count", 0)
    state.setdefault("notify_recheck_key", None)
    state.setdefault("last_action_required_notify", None)
    state.setdefault("analyzer_spooled_terminal_fingerprints", [])

    tasks_manifest = load_manifest(task_manifest_path)
    if not tasks_manifest:
        tasks_manifest = load_task_file_manifest(task_file_path)

    while True:
        native_job_dir = None
        if harbor_job_dir_file is not None and harbor_job_dir_file.is_file():
            raw_job_dir = harbor_job_dir_file.read_text(encoding="utf-8").strip()
            native_job_dir = Path(raw_job_dir) if raw_job_dir else None
        native_snapshot = load_harbor_job_snapshot(native_job_dir) if native_job_dir else None
        total = total_override
        running = running_override if running_override is not None else 0
        claimed = claimed_override
        remaining = remaining_override
        task_records = None
        terminal_artifacts_missing = None
        run_finalized = True
        if harbor_job_dir_file is not None:
            if native_snapshot is None:
                # Give the native Harbor process its normal startup window. If it
                # exits without creating a job result, this becomes abnormal_exit.
                total = total if total is not None else 1
                claimed = claimed if claimed is not None else 0
                remaining = remaining if remaining is not None else total
                running = 1 if process_is_alive(harbor_pid_file) else 0
                task_records = {}
                terminal_artifacts_missing = False
                run_finalized = False
            else:
                total = native_snapshot.total
                claimed = native_snapshot.claimed
                remaining = native_snapshot.remaining
                running = native_snapshot.running
                benchmark_alive = process_is_alive(harbor_pid_file)
                running = max(running, 1) if benchmark_alive else 0
                task_records = native_snapshot.tasks
                terminal_artifacts_missing = False
                run_finalized = native_snapshot.finished and read_exit_code(harbor_exit_file) == 0
        if total is None and tasks_manifest:
            total = len(tasks_manifest)
        if claimed is None:
            next_index_candidates = [run_dir / "NEXT_INDEX_FILE"]
            if queue_dir is not None:
                next_index_candidates.append(queue_dir / "next_index")
            next_index_candidates.append(run_dir / "QUEUE_DIR" / "next_index")
            next_index = None
            for next_index_path in next_index_candidates:
                next_index = read_int(next_index_path, default=None)
                if next_index is not None:
                    break
            claimed = next_index if next_index is not None else 0
            if claimed is not None:
                claimed = max(0, claimed - 1)
        if remaining is None and total is not None and claimed is not None:
            remaining = max(0, total - claimed)

        if running_override is None and harbor_job_dir_file is None:
            # fallback from worker-*.current under queue_dir
            qdir = queue_dir or run_dir / "QUEUE_DIR"
            if qdir.exists():
                running = count_running_workers(qdir)

        adaptive_S = int(state.get("adaptive_S") or S_default)
        env_events_raw = read_first_existing_text(
            [
                run_dir / "environment_events.json",
                run_dir / "online-analysis" / "environment-summary.json",
            ]
        )
        output, action, history, extras = evaluate_once(
            run_dir=run_dir,
            done_path=done_path,
            failed_path=failed_path,
            tasks_manifest=tasks_manifest,
            total=total,
            claimed=claimed,
            remaining=remaining,
            running=running,
            environment_events_raw=env_events_raw,
            S=adaptive_S,
            startup_grace=startup_grace,
            configured_timeout=configured_timeout,
            max_retries=max_retries,
            state=state,
            include_unknown_not_complete=include_unknown_not_complete,
            task_records=task_records,
            terminal_artifacts_missing=terminal_artifacts_missing,
            run_finalized=run_finalized,
        )
        state["last_progress_ts"] = extras.get("last_progress_ts")
        state["run_start_ts"] = extras.get("run_start_ts")

        # adaptive S
        benchmark_status = output["benchmark_status"]
        consecutive_stall = state.get("consecutive_stall", 0)
        status_reason = str(output.get("status_reason") or "")
        if benchmark_status == "running" and status_reason == "progressing":
            adaptive_S = max(S_min, int(adaptive_S * 0.9))
        else:
            consecutive_stall = 0
        state["adaptive_S"] = adaptive_S
        state["consecutive_stall"] = consecutive_stall

        if action["type"] == "restart":
            if not restart_cmd:
                    output["action"] = {
                        "type": "notify",
                        "retry_count": state.get("retry_count", 0),
                        "reason": "restart_needed_but_restart_cmd_missing",
                        "control_type": "restart",
                        "control_attempted": False,
                        "external_control_performed": False,
                    }
            else:
                control_argv, control_error = build_control_argv(restart_cmd, run_dir, "restart")
                if control_error or control_argv is None:
                    output["action"] = {
                        "type": "notify",
                        "retry_count": state.get("retry_count", 0),
                        "reason": control_error or "restart_cmd_invalid",
                        "control_type": "restart",
                        "control_attempted": False,
                        "external_control_performed": False,
                    }
                else:
                    state["retry_count"] = state.get("retry_count", 0) + 1
                    try:
                        result = subprocess.run(
                            control_argv,
                            cwd=run_dir,
                            shell=False,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            timeout=120,
                            check=False,
                        )
                        if result.returncode == 0:
                            control_ts = now_ts()
                            state["last_progress_ts"] = control_ts
                            history = [
                                {
                                    "ts": control_ts,
                                    "finished": output["finished"],
                                    "running": output["running"],
                                    "remaining": output.get("unclaimed_remaining"),
                                    "unfinished": output.get("unfinished"),
                                    "status": "restart_executed",
                                }
                            ]
                            state["run_start_ts"] = control_ts
                            output["action"] = {
                                "type": "restart",
                                "retry_count": state["retry_count"],
                                "reason": action["reason"],
                                "control_type": "restart",
                                "control_exit_code": result.returncode,
                                "control_attempted": True,
                                "external_control_performed": True,
                            }
                        else:
                            output["action"] = {
                                "type": "notify",
                                "retry_count": state["retry_count"],
                                "reason": f"restart_failed_exit_code={result.returncode}",
                                "control_type": "restart",
                                "control_exit_code": result.returncode,
                                "control_attempted": True,
                                "external_control_performed": True,
                            }
                        output["control_stdout"] = result.stdout[-2000:]
                    except subprocess.TimeoutExpired as exc:
                        output["action"] = {
                            "type": "notify",
                            "retry_count": state["retry_count"],
                            "reason": "restart_failed_timeout",
                            "control_type": "restart",
                            "control_error": str(exc),
                            "control_attempted": True,
                            "external_control_performed": True,
                        }
                    except Exception as exc:  # noqa: BLE001 - control boundary reports failures
                        output["action"] = {
                            "type": "notify",
                            "retry_count": state["retry_count"],
                            "reason": "restart_failed_exception",
                            "control_type": "restart",
                            "control_error": str(exc),
                            "control_attempted": True,
                            "external_control_performed": False,
                        }
        elif action["type"] == "stop":
            output["action"] = action
            if stop_cmd:
                control_argv, control_error = build_control_argv(stop_cmd, run_dir, "stop")
                if control_error or control_argv is None:
                    output["action"] = {
                        "type": "notify",
                        "retry_count": state.get("retry_count", 0),
                        "reason": control_error or "stop_cmd_invalid",
                        "control_type": "stop",
                        "control_attempted": False,
                        "external_control_performed": False,
                    }
                else:
                    try:
                        result = subprocess.run(
                            control_argv,
                            cwd=run_dir,
                            shell=False,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            timeout=120,
                            check=False,
                        )
                        output["action"] = {
                            "type": "stop" if result.returncode == 0 else "notify",
                            "retry_count": state.get("retry_count", 0),
                            "reason": action["reason"] if result.returncode == 0 else f"stop_failed_exit_code={result.returncode}",
                            "control_type": "stop",
                            "control_exit_code": result.returncode,
                            "control_attempted": True,
                            "external_control_performed": True,
                        }
                        output["control_stdout"] = result.stdout[-2000:]
                    except subprocess.TimeoutExpired as exc:
                        output["action"] = {
                            "type": "notify",
                            "retry_count": state.get("retry_count", 0),
                            "reason": "stop_failed_timeout",
                            "control_type": "stop",
                            "control_error": str(exc),
                            "control_attempted": True,
                            "external_control_performed": True,
                        }
                    except Exception as exc:  # noqa: BLE001 - control boundary reports failures
                        output["action"] = {
                            "type": "notify",
                            "retry_count": state.get("retry_count", 0),
                            "reason": "stop_failed_exception",
                            "control_type": "stop",
                            "control_error": str(exc),
                            "control_attempted": True,
                            "external_control_performed": False,
                        }
        else:
            output["action"] = action

        output["user_notify"] = build_user_notify(
            output=output,
            action=output["action"],
            max_retries=max_retries,
            run_dir=run_dir,
            queue_dir=queue_dir,
            output_path=output_path,
        )
        output["analyzer_handover"] = build_analyzer_handover(
            output,
            run_dir=run_dir,
            queue_dir=queue_dir,
        )
        output["runner_action"] = build_runner_action(
            action=output["action"],
            benchmark_status=str(output.get("benchmark_status") or "blocked"),
            status_reason=str(output.get("status_reason") or ""),
            evidence=output.get("evidence") if isinstance(output.get("evidence"), dict) else {},
            max_retries=max_retries,
        )
        action_type = str(output["action"].get("type") or "")
        notify_incident_key = None
        if action_type == "notify":
            notify_incident_key = build_notify_incident_key(output, output["action"])
            if state.get("notify_recheck_key") != notify_incident_key:
                state["notify_recheck_key"] = notify_incident_key
                state["notify_recheck_count"] = 0
            state["last_action_required_notify"] = {
                "timestamp": output.get("timestamp"),
                "benchmark_status": output.get("benchmark_status"),
                "status_reason": output.get("status_reason"),
                "monitor_action": action_type,
                "retry_count": output["action"].get("retry_count"),
                "task_summary": output.get("task_summary"),
            }
        elif (
            action_type in {"restart", "stop"}
            or output.get("benchmark_status") == "completed"
            or output.get("status_reason") == "progressing"
            or int(output.get("finished_delta") or 0) > 0
        ):
            state["notify_recheck_count"] = 0
            state["notify_recheck_key"] = None

        notify_recheck_allowed = (
            action_type == "notify"
            and output.get("benchmark_status") == "blocked"
            and output.get("status_reason") in {"abnormal_exit", "stalled"}
            and int(state.get("notify_recheck_count", 0) or 0) < 1
        )
        previous_notify = state.get("last_action_required_notify")
        if previous_notify and action_type != "notify":
            output["previous_action_required_notify"] = previous_notify

        if action_type in {"wait", "restart"}:
            output["monitor_follow_decision"] = "continue"
        elif action_type == "stop":
            output["monitor_follow_decision"] = "stop_completed"
        elif (
            action_type == "notify"
            and output.get("benchmark_status") == "running"
            and output.get("status_reason") == "timeout_reached"
        ):
            output["monitor_follow_decision"] = "continue"
        elif notify_recheck_allowed:
            state["notify_recheck_count"] = int(state.get("notify_recheck_count", 0) or 0) + 1
            output["notify_recheck"] = {
                "enabled": True,
                "reason": "action_required_notify_will_be_rechecked_once",
                "incident_key": notify_incident_key,
                "count": state["notify_recheck_count"],
                "limit": 1,
            }
            output["monitor_follow_decision"] = "continue"
        else:
            output["monitor_follow_decision"] = "stop_action_required"
        output["state"] = {
            "retry_count": state["retry_count"],
            "adaptive_S": adaptive_S,
            "history_len": len(history),
            "notify_recheck_count": state["notify_recheck_count"],
        }

        state["history"] = history
        output_json = json.dumps(output, ensure_ascii=False, indent=2)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output_json + "\n", encoding="utf-8")
        write_json(user_report_output, output["user_notify"])
        analyzer_handover = output["analyzer_handover"]
        if analyzer_handover_output and analyzer_handover.get("should_run_analyzer"):
            spooled_raw = state.get("analyzer_spooled_terminal_fingerprints")
            spooled = {
                str(value)
                for value in spooled_raw
                if isinstance(value, str) and value
            } if isinstance(spooled_raw, list) else set()
            tasks = analyzer_handover.get("tasks")
            new_tasks: list[dict[str, Any]] = []
            new_fingerprints: list[str] = []
            if isinstance(tasks, list):
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    fingerprint = str(task.get("terminal_fingerprint") or "")
                    if not fingerprint or fingerprint in spooled:
                        continue
                    new_tasks.append(task)
                    new_fingerprints.append(fingerprint)
            if new_tasks:
                handoff = build_analyzer_handover_for_tasks(analyzer_handover, new_tasks)
                handover_id = str(handoff.get("handover_id") or "")
                if handover_id:
                    spool_path = analyzer_handover_output.parent / "analyzer-handoffs" / f"{handover_id}.json"
                    if not spool_path.exists():
                        write_json(spool_path, handoff)
                    state["analyzer_spooled_terminal_fingerprints"] = sorted(
                        spooled | set(new_fingerprints)
                    )
        write_json(analyzer_handover_output, analyzer_handover)
        write_json(runner_action_output, output["runner_action"])
        print(output_json)
        save_state(state_path, state)

        if loop_once:
            break
        if output["monitor_follow_decision"] in {"stop_completed", "stop_action_required"}:
            break
        time.sleep(poll_interval)
