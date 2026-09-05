#!/usr/bin/env python3
"""Generate one run's agent environment and Harbor MCP configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--pi-extension-source", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    runtime_dir = run_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    agent_env = runtime_dir / "agent.env"
    mcp_config = runtime_dir / "mcp.json"
    manifest = runtime_dir / "run.json"
    pi_extensions = runtime_dir / "pi-extensions"
    extension_payloads: dict[str, bytes] = {}
    if args.pi_extension_source and args.pi_extension_source.is_dir():
        for source in sorted(args.pi_extension_source.glob("*.ts")):
            extension_payloads[source.name] = source.read_bytes()
    benchmark_extensions = Path(__file__).resolve().parents[1] / "integrations" / "pi"
    for source in sorted(benchmark_extensions.glob("*.ts")):
        extension_payloads[source.name] = source.read_bytes()
    pi_extensions.mkdir(parents=True, exist_ok=True)
    for stale in pi_extensions.glob("*.ts"):
        stale.unlink()
    for name, payload in extension_payloads.items():
        (pi_extensions / name).write_bytes(payload)

    env_values = {
        "BROWSECOMP_MCP_URL": args.mcp_url,
        "BROWSECOMP_RUN_ID": args.run_id,
        "BROWSECOMP_EVENT_LOG": "/logs/agent/browsecomp-events.jsonl",
    }
    for key, value in env_values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"unsafe newline in {key}")
    agent_env.write_text("".join(f"{key}={value}\n" for key, value in env_values.items()), encoding="utf-8")
    atomic_write_json(
        mcp_config,
        {"mcpServers": {"browsecomp": {"type": "http", "url": args.mcp_url}}},
    )
    atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "benchmark": "browsecomp-plus",
            "run_id": args.run_id,
            "dataset_root": str(args.dataset_root.resolve()),
            "mcp_url": args.mcp_url,
            "agent_env": str(agent_env),
            "mcp_config": str(mcp_config),
            "pi_extension_dir": str(pi_extensions),
            "judge_mode": os.environ.get("BROWSECOMP_JUDGE_MODE", "none"),
        },
    )
    print(json.dumps({"agent_env": str(agent_env), "mcp_config": str(mcp_config), "manifest": str(manifest), "pi_extension_dir": str(pi_extensions)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
