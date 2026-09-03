#!/usr/bin/env python3
"""Run one task through DSH's version-matched ``sdk-minimal`` profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from deepseek_harness import DeepSeekHarness


def main() -> None:
    """Launch the official profile and print its final assistant response."""
    parser = argparse.ArgumentParser()
    configured_home = os.environ.get("DSH_HOME", "")
    parser.add_argument("prompt", help="Task for the minimal agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=Path(configured_home) if configured_home.strip() else None,
    )
    parser.add_argument("--dsh-bin", type=Path, required=True)
    parser.add_argument("--profile", default="sdk-minimal")
    parser.add_argument("--session-id")
    parser.add_argument("--provider", default="deepseek-official")
    parser.add_argument(
        "--model",
        default=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
    )
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--trace-path", type=Path, required=True)
    args = parser.parse_args()
    if args.dsh_home is None:
        parser.error("--dsh-home or a non-empty DSH_HOME is required")

    trace_path = args.trace_path.resolve()
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("w", encoding="utf-8") as trace:

        def record(notification: Any) -> None:
            trace.write(
                json.dumps(
                    {
                        "method": notification.method,
                        "payload": notification.payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            trace.flush()

        with DeepSeekHarness(
            provider=args.provider,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_tokens=args.max_tokens,
            cwd=str(args.workspace.resolve()),
            dsh_home=str(args.dsh_home.resolve()),
            dsh_bin=str(args.dsh_bin.resolve()),
            profile=args.profile,
        ) as harness:
            result = harness.run(
                args.prompt,
                session_id=args.session_id,
                on_notification=record,
            )
        trace.write(
            json.dumps(
                {
                    "method": "agent-fleet/run-result",
                    "payload": {
                        "final_response": result.final_response,
                        "finish_reason": result.finish_reason,
                        "session_id": result.session_id,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
    print(result.final_response)


if __name__ == "__main__":
    main()
