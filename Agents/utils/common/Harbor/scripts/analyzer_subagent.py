#!/usr/bin/env python3
"""Run the Harbor Analyzer entrypoint that launches Pi."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from harbor_analyzer.io import load_json, stable_hash, utc_now, write_json_atomic
from harbor_analyzer.runner import AnalyzerConfig, run_handover


FOLLOW_MAX_BACKOFF_SECONDS = 300.0
FOLLOW_MAX_FAILURE_ATTEMPTS = 3


def _default_base_url() -> str:
    value = (
        os.environ.get("HARBOR_ANALYZER_BASE_URL") or os.environ.get("BASE_URL") or ""
    ).rstrip("/")
    if value and not value.endswith("/v1"):
        value += "/v1"
    return value


def _default_model() -> str:
    return os.environ.get("HARBOR_ANALYZER_MODEL") or os.environ.get("MODEL") or ""


def _ensure_analyzer_env_defaults() -> None:
    if not os.environ.get("HARBOR_ANALYZER_API_KEY") and os.environ.get("API_KEY"):
        os.environ["HARBOR_ANALYZER_API_KEY"] = os.environ["API_KEY"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handover", required=True, type=Path)
    parser.add_argument(
        "--handoff-dir",
        type=Path,
        help="Monitor append-only handoff spool (default: sibling analyzer-handoffs)",
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument(
        "--pi-provider",
        default=os.environ.get("HARBOR_ANALYZER_PI_PROVIDER", "harbor-analyzer"),
    )
    parser.add_argument("--pi-model", default=_default_model())
    parser.add_argument("--pi-base-url", default=_default_base_url())
    parser.add_argument("--pi-api-key-env", default="HARBOR_ANALYZER_API_KEY")
    parser.add_argument("--timeout", type=int, default=900, help="Per-task Pi timeout in seconds")
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="Maximum number of per-task Pi subagents to run concurrently",
    )
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    return args


def _config(args: argparse.Namespace) -> AnalyzerConfig:
    run_dir = args.run_dir.resolve()
    queue_dir = args.queue_dir
    if queue_dir is None:
        candidate = run_dir / "queue" / args.agent
        queue_dir = candidate if candidate.exists() else None
    output_dir = (args.output_dir or run_dir / "analyzer").resolve()
    return AnalyzerConfig(
        run_dir=run_dir,
        queue_dir=queue_dir.resolve() if queue_dir else None,
        output_dir=output_dir,
        run_id=args.run_id,
        pi_bin=args.pi_bin,
        provider=args.pi_provider,
        model=args.pi_model,
        base_url=args.pi_base_url,
        api_key_env=args.pi_api_key_env,
        timeout_seconds=args.timeout,
        max_concurrency=args.max_concurrency,
    )


def _load_follow_state(state_path: Path) -> tuple[set[str], dict[str, dict[str, Any]]]:
    if not state_path.is_file():
        return set(), {}
    try:
        state = load_json(state_path)
    except ValueError:
        return set(), {}
    values = state.get("attempted_handover_keys")
    if values is None:
        values = state.get("attempted_handover_ids")
    processed = {
        str(value)
        for value in values
        if isinstance(value, str) and value
    } if isinstance(values, list) else set()
    failed_raw = state.get("failed_handover_retries")
    failed: dict[str, dict[str, Any]] = {}
    if isinstance(failed_raw, dict):
        for handover_id, record in failed_raw.items():
            if isinstance(handover_id, str) and handover_id and isinstance(record, dict):
                failed[handover_id] = dict(record)
    return processed, failed


def _save_follow_state(
    state_path: Path,
    *,
    processed: set[str],
    failed: dict[str, dict[str, Any]],
) -> None:
    write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "kind": "harbor_analyzer_follow_state",
            "updated_at": utc_now(),
            "attempted_handover_keys": sorted(processed),
            "failed_handover_retries": failed,
        },
    )


def _record_follow_failure(
    failed: dict[str, dict[str, Any]],
    *,
    handover_id: str,
    exit_code: int,
    poll_interval: float,
) -> None:
    previous = failed.get(handover_id)
    previous_count = (
        int(previous.get("attempt_count") or 0)
        if isinstance(previous, dict)
        else 0
    )
    attempt_count = previous_count + 1
    delay = min(max(poll_interval, 1.0) * (2 ** (attempt_count - 1)), FOLLOW_MAX_BACKOFF_SECONDS)
    now = time.time()
    retry_exhausted = attempt_count >= FOLLOW_MAX_FAILURE_ATTEMPTS
    failed[handover_id] = {
        "attempt_count": attempt_count,
        "last_exit_code": exit_code,
        "last_failed_at": now,
        "last_failed_at_utc": utc_now(),
        "next_retry_at": None if retry_exhausted else now + delay,
        "retry_delay_seconds": None if retry_exhausted else delay,
        "retry_exhausted": retry_exhausted,
        "max_failure_attempts": FOLLOW_MAX_FAILURE_ATTEMPTS,
    }


def _handover_follow_key(handover: dict[str, Any]) -> str:
    handover_id = str(handover.get("handover_id") or "")
    tasks = handover.get("tasks")
    if not isinstance(tasks, list):
        return handover_id
    identities: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        identities.append(
            {
                "task_index": task.get("task_index"),
                "task_name": task.get("task_name"),
                "attempt_id": task.get("attempt_id"),
                "terminal_fingerprint": task.get("terminal_fingerprint"),
            }
        )
    if not identities:
        return handover_id
    return f"{handover_id}:{stable_hash(identities)}"


def _pending_handovers(
    *,
    latest_path: Path,
    handoff_dir: Path,
    processed: set[str],
    failed: dict[str, dict[str, Any]],
    now: float,
) -> list[tuple[dict[str, Any], Path, str]]:
    candidates = sorted(handoff_dir.glob("*.json")) if handoff_dir.is_dir() else []
    if latest_path.is_file():
        candidates.append(latest_path)
    unique: dict[str, tuple[dict[str, Any], Path, str]] = {}
    for path in candidates:
        try:
            handover = load_json(path)
        except ValueError:
            continue
        handover_id = str(handover.get("handover_id") or "")
        follow_key = _handover_follow_key(handover)
        if not handover_id or follow_key in processed or follow_key in unique:
            continue
        failed_record = failed.get(follow_key)
        if isinstance(failed_record, dict) and failed_record.get("retry_exhausted") is True:
            continue
        next_retry_at = (
            failed_record.get("next_retry_at")
            if isinstance(failed_record, dict)
            else None
        )
        if isinstance(next_retry_at, (int, float)) and next_retry_at > now:
            continue
        unique[follow_key] = (handover, path, follow_key)
    return sorted(
        unique.values(),
        key=lambda item: (str(item[0].get("generated_at") or ""), str(item[1])),
    )


def _run_one(handover: dict[str, Any], source_path: Path, config: AnalyzerConfig) -> int:
    aggregate, exit_code = run_handover(handover, handover_path=source_path, config=config)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def main() -> int:
    _ensure_analyzer_env_defaults()
    args = parse_args()
    config = _config(args)
    if not args.follow:
        return _run_one(load_json(args.handover), args.handover, config)

    handoff_dir = (args.handoff_dir or args.handover.parent / "analyzer-handoffs").resolve()
    state_path = config.output_dir / ".analyzer_state.json"
    processed, failed = _load_follow_state(state_path)
    while True:
        pending = _pending_handovers(
            latest_path=args.handover,
            handoff_dir=handoff_dir,
            processed=processed,
            failed=failed,
            now=time.time(),
        )
        if not pending:
            time.sleep(args.poll_interval)
            continue
        for handover, source_path, follow_key in pending:
            try:
                exit_code = _run_one(handover, source_path, config)
            except ValueError as exc:
                print(f"Analyzer rejected {source_path}: {exc}", file=sys.stderr)
                _record_follow_failure(
                    failed,
                    handover_id=follow_key,
                    exit_code=2,
                    poll_interval=args.poll_interval,
                )
                _save_follow_state(state_path, processed=processed, failed=failed)
                continue
            if exit_code == 0:
                processed.add(follow_key)
                failed.pop(follow_key, None)
                _save_follow_state(state_path, processed=processed, failed=failed)
            else:
                _record_follow_failure(
                    failed,
                    handover_id=follow_key,
                    exit_code=exit_code,
                    poll_interval=args.poll_interval,
                )
                _save_follow_state(state_path, processed=processed, failed=failed)
                print(
                    f"Analyzer did not mark {source_path} processed because exit_code={exit_code}",
                    file=sys.stderr,
                )
        # Follow mode remains alive; per-handover failures are written as analyzer-error JSON.


if __name__ == "__main__":
    raise SystemExit(main())
