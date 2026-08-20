"""Select stable smoke tasks from a validated Fix Plan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..artifact_io import write_json, write_text
from ..validation import task_key


def sort_task_index(value: str) -> tuple[int, int | str]:
    try:
        return 0, int(value)
    except ValueError:
        return 1, value


def plan_exec_map(exec_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(plan["plan_id"]): plan for plan in exec_result["plans"]}


def plan_tasks(fix_plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {str(plan["plan_id"]): list(plan["task_list"]) for plan in fix_plan["plans"]}


def selection_hash(source: dict[str, Any], plan_id: str, task: dict[str, Any]) -> str:
    raw = json.dumps(
        {"source": source, "plan_id": plan_id, "task": task_key(task)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def build_smoke_selection(
    fix_plan: dict[str, Any],
    exec_plans: dict[str, dict[str, Any]],
    *,
    limit_per_plan: int,
    output_dir: Path,
) -> dict[str, Any]:
    selected_tasks: list[dict[str, Any]] = []
    selected_task_names: set[str] = set()
    plan_records: list[dict[str, Any]] = []
    source = fix_plan["source"]

    for plan_id, tasks in plan_tasks(fix_plan).items():
        exec_status = str(exec_plans[plan_id]["status"])
        ranked = sorted(
            ((selection_hash(source, plan_id, task), task) for task in tasks),
            key=lambda item: item[0],
        )
        selected: list[tuple[str, dict[str, Any]]] = []
        if exec_status == "success":
            for digest, task in ranked:
                task_name = str(task["task_name"])
                if task_name in selected_task_names:
                    continue
                selected.append((digest, task))
                selected_task_names.add(task_name)
                if len(selected) == limit_per_plan:
                    break
        selected_keys = {task_key(task) for _, task in selected}
        for digest, task in selected:
            selected_tasks.append(
                {
                    "plan_id": plan_id,
                    "original_task_index": str(task["task_index"]),
                    "task_name": str(task["task_name"]),
                    "attempt_id": task["attempt_id"],
                    "selection_hash": digest,
                }
            )
        plan_records.append(
            {
                "plan_id": plan_id,
                "exec_status": exec_status,
                "total_task_count": len(tasks),
                "sampled_task_indexes": sorted(
                    [str(task["task_index"]) for _, task in selected],
                    key=sort_task_index,
                ),
                "unsampled_task_indexes": sorted(
                    [
                        str(task["task_index"])
                        for task in tasks
                        if task_key(task) not in selected_keys
                    ],
                    key=sort_task_index,
                ),
            }
        )

    selected_tasks.sort(
        key=lambda task: (
            task["selection_hash"],
            sort_task_index(task["original_task_index"]),
        )
    )
    for index, task in enumerate(selected_tasks, start=1):
        task["smoke_task_index"] = str(index)

    task_source = output_dir / "verification-smoke-tasks.txt"
    selection_path = output_dir / "verification-smoke-selection.json"
    write_text(
        task_source, "".join(f"{task['task_name']}\n" for task in selected_tasks)
    )
    payload = {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_smoke_selection",
        "verification_mode": "smoke_test",
        "selection_policy": "stable_hash",
        "limit_per_plan": limit_per_plan,
        "source": {
            "task_source_path": str(task_source),
            "selection_path": str(selection_path),
        },
        "plans": plan_records,
        "tasks": selected_tasks,
        "sampled_task_count": len(selected_tasks),
    }
    write_json(selection_path, payload)
    return payload
