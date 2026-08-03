"""Select a controller action from one Harbor monitor observation."""

from __future__ import annotations

from typing import Any


def decide_action(
    observation: dict[str, Any],
    *,
    retry_count: int,
    max_retries: int,
) -> dict[str, Any]:
    """Select an observation action without executing human control decisions."""

    benchmark_status = observation.get("benchmark_status")
    reason = str(observation.get("status_reason") or "")
    running = int(observation.get("running") or 0)
    unfinished = int(observation.get("unfinished") or 0)
    stalled_duration_reached = bool(observation.get("stalled_duration_reached"))
    action_type = "wait"
    allowed_decisions: list[str] = []
    if (
        benchmark_status == "blocked"
        and reason == "abnormal_exit"
        and running == 0
        and unfinished > 0
    ):
        action_type = "notify"
        if retry_count < max_retries:
            allowed_decisions = ["restart"]
        else:
            reason = f"restart_limit_reached(max_retries={max_retries})"
    elif (
        benchmark_status == "running"
        and running > 0
        and unfinished > 0
        and (
            reason == "timeout_reached"
            or (reason == "suspected_stalled" and stalled_duration_reached)
        )
    ):
        action_type = "notify"
        allowed_decisions = ["wait", "stop"]
    elif benchmark_status == "blocked":
        action_type = "notify"

    if benchmark_status == "completed":
        controller_status = "completed"
    elif action_type == "notify":
        controller_status = "awaiting_user_decision"
    else:
        controller_status = "observing"

    action = {
        "type": action_type,
        "retry_count": retry_count,
        "reason": reason,
        "allowed_decisions": allowed_decisions,
        "decision_required": action_type == "notify",
        "controller_status": controller_status,
        "external_control_performed": False,
        "compatibility_note": (
            "Harbor controller decisions are command-based and runner-neutral; "
            "restart and stop require an explicit user decision."
        ),
    }
    return action
