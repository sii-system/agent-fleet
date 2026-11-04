#!/usr/bin/env python3
"""CLI entrypoint for the Harbor benchmark monitor."""

from __future__ import annotations

import argparse
from pathlib import Path

from harbor_monitor.runner import run_loop


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harbor monitor CLI")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--done", type=Path)
    parser.add_argument("--failed", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--harbor-job-dir-file", type=Path)
    parser.add_argument("--harbor-pid-file", type=Path)
    parser.add_argument("--harbor-exit-file", type=Path)
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--task-manifest", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--total", type=int, default=None)
    parser.add_argument("--claimed", type=int, default=None)
    parser.add_argument("--remaining", type=int, default=None)
    parser.add_argument("--running", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--user-report-output", type=Path, default=None)
    parser.add_argument("--analyzer-handover-output", type=Path, default=None)
    parser.add_argument("--runner-action-output", type=Path, default=None)
    parser.add_argument("--restart-cmd", default=None)
    parser.add_argument("--stop-cmd", default=None)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--stall-seconds", type=int, default=1800)
    parser.add_argument("--stall-min", type=int, default=900)
    parser.add_argument(
        "--stall-max", type=int, help="Deprecated compatibility option; has no effect."
    )
    parser.add_argument("--startup-grace", type=int, default=30)
    parser.add_argument("--configured-timeout", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--handoff-not-complete-on-no-progress", dest="handoff_not_complete_on_no_progress", action="store_true")
    parser.add_argument("--no-handoff-not-complete-on-no-progress", dest="handoff_not_complete_on_no_progress", action="store_false")
    parser.set_defaults(handoff_not_complete_on_no_progress=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_once = not args.follow
    manifest_path = args.task_manifest or args.manifest
    queue_dir = args.queue_dir
    if queue_dir is None:
        candidate = args.run_dir / "queue" / args.agent
        if candidate.exists():
            queue_dir = candidate
    task_file_path = args.task_file or (args.run_dir / "tasks.txt")
    done_path = args.done or (queue_dir / "done.txt" if queue_dir else args.run_dir / "done.txt")
    failed_path = args.failed or (queue_dir / "failed.txt" if queue_dir else args.run_dir / "failed.txt")

    run_loop(
        run_dir=args.run_dir,
        done_path=done_path,
        failed_path=failed_path,
        queue_dir=queue_dir,
        task_manifest_path=manifest_path,
        task_file_path=task_file_path,
        restart_cmd=args.restart_cmd,
        stop_cmd=args.stop_cmd,
        output_path=args.output,
        poll_interval=args.interval,
        max_retries=args.max_retries,
        S_default=args.stall_seconds,
        S_min=args.stall_min,
        startup_grace=args.startup_grace,
        configured_timeout=args.configured_timeout,
        total_override=args.total,
        running_override=args.running,
        claimed_override=args.claimed,
        remaining_override=args.remaining,
        user_report_output=args.user_report_output,
        analyzer_handover_output=args.analyzer_handover_output,
        runner_action_output=args.runner_action_output,
        loop_once=run_once,
        include_unknown_not_complete=args.handoff_not_complete_on_no_progress,
        harbor_job_dir_file=args.harbor_job_dir_file,
        harbor_pid_file=args.harbor_pid_file,
        harbor_exit_file=args.harbor_exit_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
