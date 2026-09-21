"""Adapt persisted rollout queues to the shared Harbor summary publisher."""

from __future__ import annotations

import html
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def find_rollout_queues(run_dir: Path) -> list[Path]:
    roots = [run_dir, run_dir / "rl-queue", *run_dir.glob("runtime/*/rl-queue")]
    candidates = [*roots, *(queue for root in roots for queue in (root / "jobs").glob("*"))]
    return sorted({
        path for path in candidates
        if all((path / state).is_dir() for state in ("pending", "active", "results"))
    })


def _read_record(path: Path, state: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A worker can move a pending request to active while we scan.
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read RL artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"Expected an RL JSON object in {path}")
    reward = payload.get("reward")
    if state == "finished":
        if not isinstance(payload.get("ok"), bool):
            raise ValueError(f"Missing or invalid RL execution outcome in {path}")
        if reward is not None and (
            not isinstance(reward, (int, float)) or not math.isfinite(reward)
        ):
            raise ValueError(f"Invalid RL reward in {path}")
    exception = payload.get("exception_info")
    exception_type = exception.get("exception_type") if isinstance(exception, dict) else None
    return {
        "state": state,
        "submission": str(payload.get("ray_submission_id") or path.parent.parent.name),
        "ok": payload.get("ok"),
        "reward": reward,
        "exception": str(exception_type) if exception_type else None,
    }


def build_rollout_summary(queues: list[Path]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    submissions: dict[str, Counter[str]] = {}
    rewards: Counter[str] = Counter()
    exceptions: Counter[str] = Counter()
    reward_sum = 0.0
    for queue in queues:
        records = {}
        # Finished results win over the active request briefly left behind by
        # the worker. Scope filenames to their queue, since submissions reuse IDs.
        for directory, state in (("pending", "queued"), ("active", "active"), ("results", "finished")):
            for path in sorted((queue / directory).glob("*.json")):
                record = _read_record(path, state)
                if record is not None:
                    records[path.name] = record
        for record in records.values():
            state = record["state"]
            submission = submissions.setdefault(record["submission"], Counter())
            totals[state] += 1
            submission[state] += 1
            if state != "finished":
                continue
            outcome = "execution_succeeded" if record["ok"] else "execution_failed"
            totals[outcome] += 1
            submission[outcome] += 1
            reward = record["reward"]
            if reward is None:
                totals["missing_reward"] += 1
            else:
                totals["scored"] += 1
                reward_sum += reward
                rewards[f"{reward:g}"] += 1
            if record["exception"]:
                exceptions[record["exception"]] += 1
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        **{key: totals[key] for key in (
            "queued", "active", "finished", "execution_succeeded", "execution_failed",
            "scored", "missing_reward",
        )},
        "mean_reward": reward_sum / totals["scored"] if totals["scored"] else None,
        "rewards": dict(sorted(rewards.items())),
        "exceptions": dict(sorted(exceptions.items())),
        "submissions": {name: dict(counts) for name, counts in sorted(submissions.items())},
    }


def _cell(value: str) -> str:
    return html.escape(" ".join(value.split())).replace("|", "&#124;").replace("`", "&#96;")


def render_rollout_summary(summary: dict[str, Any]) -> str:
    mean = summary["mean_reward"]
    mean_text = f"{mean:g} ({summary['scored']} scored requests)" if mean is not None else "unavailable"
    lines = [
        "## RL Rollout", "",
        f"Snapshot: {summary['snapshot_at']}", "",
        ("This is a queue snapshot; it does not establish listener completion. "
         "Requests can change state during collection."), "",
        "| Metric | Value |", "| --- | ---: |",
    ]
    for label, key in (
        ("Queued requests", "queued"), ("Active requests", "active"),
        ("Finished requests", "finished"), ("Execution succeeded", "execution_succeeded"),
        ("Execution failed", "execution_failed"), ("Missing reward", "missing_reward"),
    ):
        lines.append(f"| {label} | {summary[key]} |")
    lines.extend([
        f"| Mean reward | {mean_text} |", "",
        ("Execution success is the worker's `ok` flag, not task success. "
         "Rewards can be zero or fractional; the mean excludes missing rewards. "
         "Execution errors are not classified as model or infrastructure failures without Analyzer evidence."),
        "", "### Submissions", "",
        "| Submission | Queued | Active | Finished | Execution failed |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for name, counts in summary["submissions"].items():
        cells = [str(counts.get(key, 0)) for key in ("queued", "active", "finished", "execution_failed")]
        lines.append(f"| {_cell(name)} | {' | '.join(cells)} |")
    if not summary["submissions"]:
        lines.append("| No recorded requests | 0 | 0 | 0 | 0 |")
    for title, column, key in (("Rewards", "Reward", "rewards"), ("Exceptions", "Exception type", "exceptions")):
        lines.extend(["", f"### {title}", "", f"| {column} | Requests |", "| --- | ---: |"])
        lines.extend(f"| {_cell(name)} | {count} |" for name, count in summary[key].items())
        if not summary[key]:
            lines.append("| None recorded | 0 |")
    return "\n".join(lines) + "\n"
