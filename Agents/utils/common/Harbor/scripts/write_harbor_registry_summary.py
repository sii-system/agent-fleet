#!/usr/bin/env python3
"""Write a final summary for a native Harbor registry run."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def format_number(value: float) -> str:
    if value.is_integer():
        return f"{value:.1f}"
    return f"{value:.6g}"


def reward_rollup(stats: dict) -> tuple[str, dict[str, int]]:
    weighted_total = 0.0
    weighted_trials = 0
    reward_counts: dict[str, int] = {}

    evals = stats.get("evals")
    if not isinstance(evals, dict):
        return "unavailable", reward_counts
    for evaluation in evals.values():
        if not isinstance(evaluation, dict):
            continue
        trials = evaluation.get("n_trials")
        weight = trials if isinstance(trials, int) and trials > 0 else 1
        metrics = evaluation.get("metrics")
        if isinstance(metrics, list):
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                for metric_name in ("reward", "mean"):
                    value = metric.get(metric_name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        weighted_total += float(value) * weight
                        weighted_trials += weight
                        break
                else:
                    continue
                break

        reward_stats = evaluation.get("reward_stats")
        rewards = reward_stats.get("reward") if isinstance(reward_stats, dict) else None
        if not isinstance(rewards, dict):
            continue
        for reward, trial_ids in rewards.items():
            if isinstance(trial_ids, list):
                reward_counts[str(reward)] = (
                    reward_counts.get(str(reward), 0) + len(trial_ids)
                )

    mean_reward = (
        format_number(weighted_total / weighted_trials)
        if weighted_trials
        else "unavailable"
    )
    return mean_reward, reward_counts


def reward_sort_key(value: str) -> tuple[int, object]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value)


def main() -> None:
    job_dir_raw, summary_raw, exit_code, dataset = sys.argv[1:]
    job_dir = Path(job_dir_raw) if job_dir_raw else None
    summary = Path(summary_raw)

    result_path = None
    result = {}
    if job_dir and job_dir.is_dir():
        candidates = sorted(
            job_dir.rglob("result.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and "n_total_trials" in payload
                and isinstance(payload.get("stats"), dict)
            ):
                result_path = candidate
                result = payload
                break

    complete = exit_code == "0" and result_path is not None
    lines = [
        f"status:      {'complete' if complete else 'failed'}",
        f"finished_at: {result.get('finished_at') or datetime.now(timezone.utc).isoformat()}",
        f"RUN_ID:      {os.environ.get('RUN_ID', '')}",
        f"AGENT:       {os.environ.get('AGENT', '')}",
        f"DATASET_NAME: {dataset}",
        f"MODEL:       {os.environ.get('HARBOR_MODEL', '')}",
        f"OUTPUT_PATH: {summary.parent}",
        f"OPIK_PROJECT_NAME: {os.environ.get('OPIK_PROJECT_NAME', '')}",
        f"harbor_exit_code: {exit_code}",
        "",
    ]

    if not complete:
        reason = (
            f"Harbor exited with code {exit_code}"
            if exit_code != "0"
            else "Harbor exited without an aggregate result"
        )
        lines.extend([f"failure_reason: {reason}", ""])

    if result_path is None:
        lines.append("Harbor result summary: unavailable")
    else:
        stats = result.get("stats") or {}
        mean_reward, reward_counts = reward_rollup(stats)
        lines.extend(
            [
                f"total:      {result.get('n_total_trials', 0)}",
                f"completed:  {stats.get('n_completed_trials', 0)}",
                f"errored:    {stats.get('n_errored_trials', 0)}",
                f"cancelled:  {stats.get('n_cancelled_trials', 0)}",
                f"retries:    {stats.get('n_retries', 0)}",
                f"mean_reward: {mean_reward}",
                "reward counts:",
            ]
        )
        if reward_counts:
            lines.extend(
                f"  reward={reward}: {reward_counts[reward]}"
                for reward in sorted(reward_counts, key=reward_sort_key)
            )
        else:
            lines.append("  unavailable")
        lines.extend(
            [
                "",
                "Harbor stats:",
                json.dumps(stats, indent=2, sort_keys=True),
            ]
        )

    lines.extend(
        [
            "",
            "result paths:",
            f"  output:          {summary.parent}",
            f"  job:             {job_dir_raw or '<unknown>'}",
            f"  result:          {result_path or '<missing>'}",
        ]
    )

    summary.parent.mkdir(parents=True, exist_ok=True)
    tmp = summary.with_name(f"{summary.name}.tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, summary)


if __name__ == "__main__":
    main()
