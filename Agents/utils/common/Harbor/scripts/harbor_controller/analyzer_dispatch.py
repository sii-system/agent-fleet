"""Dispatch deduplicated Analyzer handovers selected from monitor output."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from harbor_monitor.contracts import build_analyzer_handover_for_tasks


def dispatch_analyzer_handover(
    handover: dict[str, Any],
    *,
    latest_output: Path | None,
    state: dict[str, Any],
    write_json: Callable[[Path | None, dict[str, Any]], None],
) -> None:
    """Write new terminal task handovers once, then update the latest snapshot."""

    if latest_output and handover.get("should_run_analyzer"):
        spooled_raw = state.get("analyzer_spooled_terminal_fingerprints")
        spooled = (
            {str(value) for value in spooled_raw if isinstance(value, str) and value}
            if isinstance(spooled_raw, list)
            else set()
        )
        tasks = handover.get("tasks")
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
            dispatch = build_analyzer_handover_for_tasks(handover, new_tasks)
            handover_id = str(dispatch.get("handover_id") or "")
            if handover_id:
                spool_path = latest_output.parent / "analyzer-handoffs" / f"{handover_id}.json"
                if not spool_path.exists():
                    write_json(spool_path, dispatch)
                state["analyzer_spooled_terminal_fingerprints"] = sorted(
                    spooled | set(new_fingerprints)
                )
    write_json(latest_output, handover)
