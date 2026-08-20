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
from harbor_fixer.executor import MAX_SUMMARY_LIMIT, run_fix_exec_from_plan
from harbor_fixer.plan_generation import (
    MAX_TASK_SUMMARIES_CHARS,
    MAX_TASK_SUMMARY_CHARS,
    run_plan_generation,
    task_artifact_label,
)
from harbor_fixer.prompts import MAIN_AGENT_PROMPT, TASK_SUBAGENT_PROMPT
from harbor_fixer.report import run_report_from_paths
from harbor_fixer.report.prompt import REPORT_MAIN_AGENT_PROMPT
from harbor_fixer.validation import HARBOR_AGENTS, ValidationError
from harbor_fixer.verifier import run_verification_from_paths


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
    mode_parser = argparse.ArgumentParser(add_help=False)
    mode_parser.add_argument("--report-only", action="store_true")
    report_only, _ = mode_parser.parse_known_args()

    def mode_default(name: str, default: str) -> str:
        return default if report_only.report_only else os.environ.get(name, default)

    parser = argparse.ArgumentParser(description="Harbor Fixer MVP CLI")
    parser.add_argument(
        "--analyzer-output",
        type=Path,
        default=os.environ.get("HARBOR_ANALYZER_OUTPUT_DIR"),
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pi-bin", default=os.environ.get("HARBOR_FIXER_PI_BIN", "pi"))
    parser.add_argument(
        "--pi-provider",
        default=os.environ.get("HARBOR_FIXER_PI_PROVIDER", "harbor-fixer"),
    )
    parser.add_argument("--pi-model", default=_default_model())
    parser.add_argument("--pi-base-url", default=_default_base_url())
    parser.add_argument("--pi-api-key-env", default="HARBOR_FIXER_API_KEY")
    parser.add_argument(
        "--timeout",
        type=int,
        default=os.environ.get("HARBOR_FIXER_AGENT_TIMEOUT", "900"),
    )
    parser.add_argument(
        "--execution-timeout",
        type=int,
        default=mode_default("HARBOR_FIXER_EXECUTION_TIMEOUT", "300"),
    )
    parser.add_argument(
        "--summary-limit",
        type=int,
        default=mode_default("HARBOR_FIXER_SUMMARY_LIMIT", "4000"),
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=mode_default("HARBOR_FIXER_MAX_CONCURRENCY", "4"),
    )
    parser.add_argument(
        "--max-task-summary-chars",
        type=int,
        default=mode_default(
            "HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS", str(MAX_TASK_SUMMARY_CHARS)
        ),
    )
    parser.add_argument(
        "--max-task-summaries-chars",
        type=int,
        default=mode_default(
            "HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS", str(MAX_TASK_SUMMARIES_CHARS)
        ),
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--write-prompts", action="store_true")
    parser.add_argument("--exec-only", action="store_true")
    parser.add_argument("--fix-plan", type=Path)
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    parser.add_argument("--policy-rules", type=Path)
    parser.add_argument(
        "--policy-write-root",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--exec-result", type=Path)
    parser.add_argument("--verification-run-dir", type=Path)
    parser.add_argument("--agent", choices=sorted(HARBOR_AGENTS))
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--verification-result", type=Path)
    parser.add_argument("--baseline-run-dir", type=Path)
    parser.add_argument(
        "--baseline-monitor-policy", choices=["auto", "on", "off"], default="auto"
    )
    parser.add_argument("--rerun-command", default=None)
    parser.add_argument(
        "--rerun-timeout",
        type=int,
        default=mode_default("HARBOR_FIXER_RERUN_TIMEOUT", "600"),
    )
    parser.add_argument("--monitor-policy", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--monitor-wait-timeout", type=int, default=3600)
    parser.add_argument("--monitor-poll-interval", type=float, default=30.0)
    parser.add_argument("--verification-task-limit-per-plan", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_concurrency <= 0:
        raise SystemExit("--max-concurrency must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.execution_timeout <= 0:
        raise SystemExit("--execution-timeout must be positive")
    if args.rerun_timeout <= 0:
        raise SystemExit("--rerun-timeout must be positive")
    if not 0 < args.summary_limit <= MAX_SUMMARY_LIMIT:
        raise SystemExit(
            f"--summary-limit must be between 1 and {MAX_SUMMARY_LIMIT}"
        )
    if args.max_task_summary_chars <= 0:
        raise SystemExit("--max-task-summary-chars must be positive")
    if args.max_task_summaries_chars <= 0:
        raise SystemExit("--max-task-summaries-chars must be positive")
    if args.verification_task_limit_per_plan <= 0:
        raise SystemExit("--verification-task-limit-per-plan must be positive")
    selected_modes = [
        args.prepare_only,
        args.exec_only,
        args.verify_only,
        args.report_only,
    ]
    if sum(1 for selected in selected_modes if selected) > 1:
        raise SystemExit(
            "--prepare-only, --exec-only, --verify-only, and --report-only are mutually exclusive"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.write_prompts:
        if args.report_only:
            write_text(
                args.output_dir / "prompts" / "report-main-agent-prompt.md",
                REPORT_MAIN_AGENT_PROMPT,
            )
        else:
            write_text(
                args.output_dir / "prompts" / "task-subagent-prompt.md",
                TASK_SUBAGENT_PROMPT,
            )
            write_text(
                args.output_dir / "prompts" / "main-agent-prompt.md", MAIN_AGENT_PROMPT
            )
    if args.exec_only:
        if args.fix_plan is None:
            raise SystemExit("--fix-plan is required with --exec-only")
        result = run_fix_exec_from_plan(
            args.fix_plan,
            args.output_dir,
            args.workspace_root,
            policy_invoker=PiAgentInvoker(
                args.output_dir,
                build_pi_config(args),
            ),
            policy_rules_path=args.policy_rules,
            policy_write_roots=args.policy_write_root,
            execution_timeout_seconds=args.execution_timeout,
            summary_limit=args.summary_limit,
        )
        return 0 if result["status"] == "success" else 1
    if args.verify_only:
        if args.fix_plan is None:
            raise SystemExit("--fix-plan is required with --verify-only")
        if args.exec_result is None:
            raise SystemExit("--exec-result is required with --verify-only")
        if args.verification_run_dir is None:
            raise SystemExit("--verification-run-dir is required with --verify-only")
        try:
            result = run_verification_from_paths(
                args.fix_plan,
                args.exec_result,
                args.verification_run_dir,
                args.output_dir,
                agent=args.agent,
                rerun_command=args.rerun_command,
                rerun_timeout=args.rerun_timeout,
                monitor_policy=args.monitor_policy,
                monitor_wait_timeout=args.monitor_wait_timeout,
                monitor_poll_interval=args.monitor_poll_interval,
                verification_task_limit_per_plan=args.verification_task_limit_per_plan,
            )
        except ValidationError as exc:
            raise SystemExit(str(exc)) from None
        return 0 if result["status"] in {"fixed", "partially_fixed"} else 1
    if args.report_only:
        if args.verification_result is None:
            raise SystemExit("--verification-result is required with --report-only")
        if args.analyzer_output is None:
            raise SystemExit("--analyzer-output is required with --report-only")
        try:
            result = run_report_from_paths(
                args.verification_result,
                args.analyzer_output,
                args.output_dir,
                PiAgentInvoker(args.output_dir, build_pi_config(args)),
                baseline_run_dir=args.baseline_run_dir,
                baseline_monitor_policy=args.baseline_monitor_policy,
            )
        except ValidationError as exc:
            raise SystemExit(str(exc)) from None
        return 0 if result["summary"]["status"] == "success" else 1
    if args.analyzer_output is None:
        raise SystemExit("--analyzer-output is required unless --exec-only is used")
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
