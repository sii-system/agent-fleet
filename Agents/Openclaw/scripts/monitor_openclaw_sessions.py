#!/usr/bin/env python3
"""Aggregate session summaries across all OpenClaw fleet instances.

Imports :mod:`stream_openclaw_session` to summarize each instance, then
produces a fleet-wide overview with active/idle counts and visible sessions.

Usage::

    python monitor_openclaw_sessions.py --total-workers 4 --config-base /path/to/config --pretty
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_stream_module():
    """Load ``stream_openclaw_session.py`` from the same directory as this file."""
    module_path = Path(__file__).with_name("stream_openclaw_session.py")
    spec = importlib.util.spec_from_file_location("stream_openclaw_session", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


stream = _load_stream_module()


def summarize_fleet(
    total_workers: int,
    base_port: int,
    port_step: int,
    port_offset: int,
    config_base: str,
    container_prefix: str = "openclaw",
) -> dict[str, Any]:
    """Summarize sessions across all fleet instances.

    Iterates over instances 1 through *total_workers*, computing each
    instance's port as ``base_port + port_offset + (instance - 1) * port_step``
    and its store root as ``config_base/<instance>``.

    Returns a dict with ``total_workers``, ``active_workers``,
    ``idle_workers``, ``instances`` (all), ``active_instances`` (sorted),
    and ``visible_instances`` (active + recent, sorted).
    """
    instances: list[dict[str, Any]] = []
    active = 0

    for instance in range(1, total_workers + 1):
        port = base_port + port_offset + (instance - 1) * port_step
        store_root = Path(config_base) / str(instance)
        summary = stream.summarize_instance(
            instance=instance,
            port=port,
            store_root=store_root,
            container_name=f"{container_prefix}-{instance}",
        )
        instances.append(summary)
        if summary["state"] == "active":
            active += 1

    active_instances = [item for item in instances if item["state"] == "active"]
    active_instances.sort(key=lambda item: stream._sort_key({"updatedAt": item["updated_at"]}), reverse=True)
    visible_instances = [item for item in instances if item["state"] in {"active", "recent"}]
    visible_instances.sort(key=lambda item: stream._sort_key({"updatedAt": item["updated_at"]}), reverse=True)

    return {
        "total_workers": total_workers,
        "active_workers": active,
        "idle_workers": total_workers - active,
        "instances": instances,
        "active_instances": active_instances,
        "visible_instances": visible_instances,
    }


def render_fleet_summary(fleet: dict[str, Any], columns: int = 80, lines: int = 24) -> str:
    """Render a fleet-wide summary as a compact, terminal-friendly multi-line string.

    Shows instance counts followed by visible sessions (active + recent),
    each truncated to *columns* width, capped at *lines* total output lines.
    """
    width = max(columns, 40)
    max_lines = max(lines, 8)
    body: list[str] = [
        "OpenClaw Session Monitor",
        "",
        f"instances: {fleet['total_workers']}",
        f"active:    {fleet['active_workers']}",
        f"idle:      {fleet['idle_workers']}",
        "",
        "visible sessions:",
    ]

    visible_instances = fleet.get("visible_instances") or fleet.get("active_instances", [])
    if visible_instances:
        available = max_lines - len(body)
        for item in visible_instances[:available]:
            summary = item["latest_turns_summary"].replace("\n", " ")
            line = (
                f"{item['instance_label']} state={item['state']} turns={item['turn_count']} "
                f"status={item['status']} session={item['session_id']} "
                f"summary={summary}"
            )
            body.append(stream._truncate(line, width))
    else:
        body.append("(none)")

    return "\n".join(body[:max_lines])


def main() -> int:
    """CLI entrypoint: summarize all fleet instances and emit JSON or pretty text."""
    parser = argparse.ArgumentParser(description="Render OpenClaw fleet session overview.")
    parser.add_argument("--total-workers", type=int, required=True)
    parser.add_argument("--base-port", type=int, default=18789)
    parser.add_argument("--port-step", type=int, default=20)
    parser.add_argument("--port-offset", type=int, default=0)
    parser.add_argument("--config-base", required=True)
    parser.add_argument("--container-prefix", default="openclaw")
    parser.add_argument("--columns", type=int, default=80)
    parser.add_argument("--lines", type=int, default=24)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    fleet = summarize_fleet(
        total_workers=args.total_workers,
        base_port=args.base_port,
        port_step=args.port_step,
        port_offset=args.port_offset,
        config_base=args.config_base,
        container_prefix=args.container_prefix,
    )

    if args.pretty:
        print(render_fleet_summary(fleet, columns=args.columns, lines=args.lines))
    else:
        json.dump(fleet, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
