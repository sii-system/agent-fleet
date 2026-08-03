#!/usr/bin/env python3
"""CLI entrypoint for Harbor Fixer MVP."""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from harbor_fixer.agent_invocation import PiAgentInvoker, PiInvocationConfig
from harbor_fixer.analyzer_inputs import build_task_inputs
from harbor_fixer.artifact_io import write_json, write_text
from harbor_fixer.plan_generation import (
    MAX_TASK_SUMMARIES_CHARS,
    MAX_TASK_SUMMARY_CHARS,
    run_plan_generation,
    task_artifact_label,
)
from harbor_fixer.prompts import MAIN_AGENT_PROMPT, TASK_SUBAGENT_PROMPT


def _default_model() -> str:
    return os.environ.get("HARBOR_FIXER_MODEL") or os.environ.get("MODEL") or ""


def _default_base_url() -> str:
    return os.environ.get("HARBOR_FIXER_BASE_URL") or os.environ.get("BASE_URL") or ""


def build_pi_config(args: argparse.Namespace) -> PiInvocationConfig:
    if not os.environ.get(args.pi_api_key_env) and os.environ.get("API_KEY"):
        os.environ[args.pi_api_key_env] = os.environ["API_KEY"]
    return PiInvocationConfig(
        pi_bin=args.pi_bin,
        provider=args.pi_provider,
        model=args.pi_model,
        base_url=args.pi_base_url,
        api_key_env=args.pi_api_key_env,
        timeout_seconds=args.timeout,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harbor Fixer MVP CLI")
    parser.add_argument("--analyzer-output", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--pi-provider", default="harbor-fixer")
    parser.add_argument("--pi-model", default=_default_model())
    parser.add_argument("--pi-base-url", default=_default_base_url())
    parser.add_argument("--pi-api-key-env", default="HARBOR_FIXER_API_KEY")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument(
        "--max-task-summary-chars",
        type=int,
        default=MAX_TASK_SUMMARY_CHARS,
    )
    parser.add_argument(
        "--max-task-summaries-chars",
        type=int,
        default=MAX_TASK_SUMMARIES_CHARS,
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--write-prompts", action="store_true")
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_concurrency <= 0:
        raise SystemExit("--max-concurrency must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.max_task_summary_chars <= 0:
        raise SystemExit("--max-task-summary-chars must be positive")
    if args.max_task_summaries_chars <= 0:
        raise SystemExit("--max-task-summaries-chars must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.write_prompts:
        write_text(args.output_dir / "prompts" / "task-subagent-prompt.md", TASK_SUBAGENT_PROMPT)
        write_text(args.output_dir / "prompts" / "main-agent-prompt.md", MAIN_AGENT_PROMPT)
    if args.prepare_only:
        task_inputs, source = build_task_inputs(args.analyzer_output)
        write_json(args.output_dir / "source.json", source)
        for task_input in task_inputs:
            write_json(
                args.output_dir
                / "task-inputs"
                / f"{task_artifact_label(task_input)}.json",
                task_input,
            )
        return 0

    pi_config = build_pi_config(args)
    task_invoker = PiAgentInvoker(
        args.output_dir,
        replace(pi_config, thinking_level="off"),
    )
    main_invoker = PiAgentInvoker(args.output_dir, pi_config)
    run_plan_generation(
        args.analyzer_output,
        args.output_dir,
        task_invoker,
        main_invoker,
        max_concurrency=args.max_concurrency,
        max_task_summary_chars=args.max_task_summary_chars,
        max_task_summaries_chars=args.max_task_summaries_chars,
        workspace_root=args.workspace_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
