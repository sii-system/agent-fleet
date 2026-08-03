"""Build the two bounded context snapshots consumed by the Fix Plan agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .runtime_inventory import collect_runtime_inventory
from .workspace_evidence import collect_workspace_evidence


def collect_planning_context(
    workspace_root: Path,
    analyzer_output_path: Path,
    task_inputs: list[dict[str, Any]],
    *,
    pi_bin: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return runtime inventory and workspace evidence without changing output schemas."""

    runtime_inventory = collect_runtime_inventory(
        workspace_root,
        task_inputs,
        pi_bin=pi_bin,
    )
    workspace_evidence = collect_workspace_evidence(
        workspace_root,
        analyzer_output_path,
        task_inputs,
    )
    return runtime_inventory, workspace_evidence
