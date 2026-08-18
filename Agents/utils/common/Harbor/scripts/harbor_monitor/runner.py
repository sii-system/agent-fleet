"""Run the monitor loop and execute configured run-local controls."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from harbor_controller.analyzer_dispatch import dispatch_analyzer_handover
from harbor_controller.decision import build_decision_request_id, resolve_user_decision
from harbor_controller.executor import execute_action
from harbor_controller.policy import decide_action

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
    build_notify_incident_key,
    build_runner_action,
    build_user_notify,
)
from .evaluator import evaluate_once


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


def _prepare_user_decision_request(
    output: dict[str, object],
    action: dict[str, object],
    *,
    run_id: str,
    restart_cmd: str | None,
    stop_cmd: str | None,
) -> tuple[dict[str, object], str | None]:
    """Expose only decisions backed by the configured run-local controls."""

    restart_available = bool(restart_cmd and restart_cmd.strip())
    stop_available = bool(stop_cmd and stop_cmd.strip())
    allowed = action.get("allowed_decisions")
    action["allowed_decisions"] = [
        decision
        for decision in allowed
        if isinstance(decision, str)
        and (
            decision == "wait"
            or (decision == "restart" and restart_available)
            or (decision == "stop" and stop_available)
        )
    ] if isinstance(allowed, list) else []
    has_decisions = action.get("type") == "notify" and bool(
        action["allowed_decisions"]
    )
    action["decision_required"] = has_decisions
    if not has_decisions:
        action.pop("decision_request_id", None)
        if action.get("type") == "notify":
            action["controller_status"] = "action_required"
        return action, None

    action["controller_status"] = "awaiting_user_decision"
    incident_key = build_notify_incident_key(output, action)
    decision_request_id = build_decision_request_id(run_id, incident_key)
    action["decision_request_id"] = decision_request_id
    return action, decision_request_id


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
    state_path: Path | None = None,
) -> None:
    state_path = state_path or run_dir / ".monitor_state.json"
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
    state.setdefault("consumed_user_decision_ids", [])
    state.setdefault("deferred_user_wait", None)

    decision_path = (
        user_report_output.parent / "user-decision.json"
        if user_report_output is not None
        else run_dir / "monitor" / "user-decision.json"
    )

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
                total = total if total is not None else len(tasks_manifest) or 1
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
        output, history, extras = evaluate_once(
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

        requested_action = decide_action(
            output,
            retry_count=int(state.get("retry_count", 0) or 0),
            max_retries=max_retries,
        )
        requested_action, decision_request_id = _prepare_user_decision_request(
            output,
            requested_action,
            run_id=run_dir.name,
            restart_cmd=restart_cmd,
            stop_cmd=stop_cmd,
        )
        selected_action, user_decision = resolve_user_decision(
            requested_action,
            decision_path=decision_path,
            request_id=decision_request_id,
            run_id=run_dir.name,
            state=state,
        )
        controller_result = execute_action(
            selected_action,
            restart_cmd=restart_cmd,
            stop_cmd=stop_cmd,
            run_dir=run_dir,
            state=state,
            observation=output,
            history=history,
        )
        if user_decision is not None:
            decision_status = str(user_decision.get("status") or "")
            submitted_decision = str(user_decision.get("decision") or "")
            if decision_status == "accepted":
                control_succeeded = (
                    controller_result.action.get("type") == submitted_decision
                    and controller_result.action.get("control_exit_code") == 0
                    and controller_result.action.get("external_control_performed") is True
                )
                decision_status = "executed" if control_succeeded else "failed"
            controller_result.action.update(
                {
                    "decision_id": user_decision.get("decision_id"),
                    "decision_request_id": user_decision.get("decision_request_id"),
                    "submitted_decision": submitted_decision,
                    "decision_status": decision_status,
                }
            )
            if decision_status == "executed" and submitted_decision == "restart":
                controller_result.action["controller_status"] = "observing"
            elif decision_status == "executed" and submitted_decision == "stop":
                controller_result.action["controller_status"] = "stopped"
            elif decision_status in {"failed", "rejected"}:
                retry_action, retry_request_id = _prepare_user_decision_request(
                    output,
                    decide_action(
                        output,
                        retry_count=int(state.get("retry_count", 0) or 0),
                        max_retries=max_retries,
                    ),
                    run_id=run_dir.name,
                    restart_cmd=restart_cmd,
                    stop_cmd=stop_cmd,
                )
                controller_result.action.update(
                    {
                        "controller_status": retry_action["controller_status"],
                        "allowed_decisions": retry_action["allowed_decisions"],
                        "decision_required": retry_action["decision_required"],
                        "decision_request_id": retry_request_id,
                    }
                )
                controller_result.action["decision_error"] = (
                    user_decision.get("reason")
                    or controller_result.action.get("reason")
                    or controller_result.action.get("control_error")
                    or "control execution failed"
                )
        output["action"] = controller_result.action
        history = controller_result.history
        if controller_result.control_stdout is not None:
            output["control_stdout"] = controller_result.control_stdout

        if (
            output["action"].get("type") == "stop"
            and output["action"].get("external_control_performed") is True
            and output["action"].get("control_exit_code") == 0
        ):
            output["benchmark_status"] = "stopped"
            output["status_reason"] = "stopped_by_user"
            output["running"] = 0

        output["user_notify"] = build_user_notify(
            output=output,
            action=output["action"],
            max_retries=max_retries,
            run_dir=run_dir,
            queue_dir=queue_dir,
            output_path=output_path,
            decision_path=decision_path,
        )
        output["analyzer_handover"] = build_analyzer_handover(
            output,
            run_dir=run_dir,
            queue_dir=queue_dir,
        )
        restart_decision_pending = (
            output["action"].get("controller_status") == "awaiting_user_decision"
            and "restart" in output["action"].get("allowed_decisions", [])
        )
        if restart_decision_pending:
            output["analyzer_handover"]["should_run_analyzer"] = False
            output["analyzer_handover"]["tasks"] = []
            output["analyzer_handover"]["instruction"] = (
                "A restart decision is pending; wait for the user's decision before "
                "running Analyzer."
            )
        elif (
            output["action"].get("type") == "restart"
            and output["action"].get("external_control_performed") is True
            and output["action"].get("control_exit_code") == 0
        ):
            output["analyzer_handover"]["should_run_analyzer"] = False
            output["analyzer_handover"]["tasks"] = []
            output["analyzer_handover"]["instruction"] = (
                "The user-approved restart was executed; wait for terminal evidence "
                "from the new attempt before running Analyzer."
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

        if output.get("benchmark_status") == "completed":
            output["monitor_follow_decision"] = "stop_completed"
        elif action_type in {"wait", "restart"}:
            output["monitor_follow_decision"] = "continue"
        elif action_type == "stop":
            output["monitor_follow_decision"] = "stop_user_requested"
        elif action_type == "notify" and output["action"].get("allowed_decisions"):
            output["monitor_follow_decision"] = "continue_awaiting_user_decision"
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
        analyzer_handover = output["analyzer_handover"]
        dispatch_analyzer_handover(
            analyzer_handover,
            latest_output=analyzer_handover_output,
            state=state,
            write_json=write_json,
        )
        write_json(user_report_output, output["user_notify"])
        write_json(runner_action_output, output["runner_action"])
        print(output_json)
        save_state(state_path, state)

        if loop_once:
            break
        if output["monitor_follow_decision"] in {
            "stop_completed",
            "stop_action_required",
            "stop_user_requested",
        }:
            break
        time.sleep(poll_interval)
