#!/usr/bin/env python3
"""Decide whether a Harbor e2e run proves the fleet still works.

terminalbench21 is a Harbor registry dataset (env.sh:55,967), so the run is a
single zellij pane running run_harbor_registry.sh -> harboropik.sh, and its
final report is $OUTPUT_PATH/summary.txt as written by
scripts/write_harbor_registry_summary.py. The worker queue files done.txt and
failed.txt are touched empty on this path and never written, so they are
deliberately not read.

Errored and cancelled trials mean the harness broke. Completed trials carrying
reward 0 are model outcomes and must not fail CI.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_MAX_HARNESS_FAILURE_RATIO = 0.10

# Fields sit at column 0 with no space before the colon. Section headings in the
# same file ("reward counts:", "Harbor stats:", "result paths:") contain a space
# and so cannot be mistaken for fields.
_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")
_REWARD = re.compile(r"^[ \t]+reward=(\S+):[ \t]*(\d+)$")


def parse_summary(text: str) -> dict[str, str]:
    """Collect column-0 `key: value` fields, first occurrence winning."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD.match(line)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def parse_reward_counts(text: str) -> dict[str, int]:
    """Read the indented `reward=<value>: <count>` histogram."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = _REWARD.match(line)
        if match:
            counts[match.group(1)] = int(match.group(2))
    return counts


def int_field(fields: dict[str, str], name: str) -> int | None:
    raw = fields.get(name)
    if raw is None or not raw.lstrip("-").isdigit():
        return None
    return int(raw)


class Verdict:
    def __init__(self, passed: bool, reasons: list[str], stats: dict):
        self.passed = passed
        self.reasons = reasons
        self.stats = stats


def evaluate(
    summary_text: str | None,
    max_harness_failure_ratio: float = DEFAULT_MAX_HARNESS_FAILURE_RATIO,
    harbor_status: int = 0,
    expected_trials: int | None = None,
) -> Verdict:
    stats: dict = {}
    reasons: list[str] = []

    if summary_text is None:
        reasons.append(
            "summary.txt is missing: the run never wrote a final report"
        )
        return Verdict(False, reasons, stats)

    fields = parse_summary(summary_text)
    stats["status"] = fields.get("status", "<absent>")
    stats["mean_reward"] = fields.get("mean_reward", "unavailable")
    stats["rewards"] = parse_reward_counts(summary_text)

    if stats["status"] != "complete":
        detail = fields.get("failure_reason") or "no failure_reason recorded"
        reasons.append(
            f"Harbor status is {stats['status']!r}, not 'complete': {detail}"
        )

    exit_code = int_field(fields, "harbor_exit_code")
    stats["harbor_exit_code"] = exit_code
    if exit_code is None:
        reasons.append("summary.txt has no numeric harbor_exit_code")
    elif exit_code != 0:
        reasons.append(f"Harbor exited with code {exit_code}")

    # Independent of Harbor's own code: catches the shell or zellij dying after
    # the summary was written.
    if harbor_status != 0:
        reasons.append(
            f"the benchmark shell exited with status {harbor_status}"
        )

    total = int_field(fields, "total")
    if total is None:
        reasons.append(
            "summary.txt has no trial totals: Harbor produced no aggregate result"
        )
        return Verdict(False, reasons, stats)

    completed = int_field(fields, "completed") or 0
    errored = int_field(fields, "errored") or 0
    cancelled = int_field(fields, "cancelled") or 0
    retries = int_field(fields, "retries") or 0
    # A trial that errored and was then retried to completion is counted in
    # BOTH n_errored_trials and n_completed_trials (see the fixture in
    # Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh:118-123,
    # where total=2, completed=2, errored=1). So errored+cancelled overcounts
    # harness breakage whenever a retry succeeded, and HARBOR_MAX_RETRIES
    # defaults to 2. Count trials that never completed instead: that excludes
    # recovered retries and still catches permanent failures and cancellations.
    unresolved = max(0, total - completed)
    stats.update(
        {
            "total": total,
            "completed": completed,
            "errored": errored,
            "cancelled": cancelled,
            "retries": retries,
            "unresolved": unresolved,
        }
    )

    if total == 0:
        reasons.append(
            "no trials ran: check the taskset and --task selection"
        )
        return Verdict(False, reasons, stats)

    if expected_trials is not None and total != expected_trials:
        reasons.append(
            f"expected {expected_trials} trials but Harbor ran {total}: "
            "task selection did not reach the benchmark"
        )

    # Over-count is normal with retries, so only a shortfall means trials went
    # missing entirely.
    accounted = completed + errored + cancelled
    if accounted < total:
        reasons.append(
            f"trials unaccounted for: {accounted} of {total} recorded"
        )

    # Expressed as a count, not a rounded percentage. At the default tolerance
    # the smallest failing count on 89 trials is 9, and 9/89 rounds to 10%, so a
    # percentage would render as "10% exceeds tolerance 10%".
    allowed = int(max_harness_failure_ratio * total)
    stats["unresolved_allowed"] = allowed
    if unresolved > allowed:
        reasons.append(
            f"{unresolved} of {total} trials never completed, exceeding the "
            f"allowance of {allowed} ({errored} errored, {cancelled} cancelled, "
            f"{retries} retried)"
        )

    return Verdict(not reasons, reasons, stats)


def render_summary(verdict: Verdict) -> str:
    stats = verdict.stats
    lines = [
        f"## Harbor e2e validation: {'PASS' if verdict.passed else 'FAIL'}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Harbor status | {stats.get('status', '<absent>')} |",
        f"| Harbor exit code | {stats.get('harbor_exit_code', '<absent>')} |",
        f"| Trials | {stats.get('total', 0)} |",
        f"| Completed | {stats.get('completed', 0)} |",
        f"| Errored | {stats.get('errored', 0)} |",
        f"| Cancelled | {stats.get('cancelled', 0)} |",
        f"| Retries | {stats.get('retries', 0)} |",
        (
            "| Never completed (allowed) | "
            f"{stats.get('unresolved', 0)} "
            f"({stats.get('unresolved_allowed', 0)}) |"
        ),
        f"| Mean reward | {stats.get('mean_reward', 'unavailable')} |",
        "",
    ]

    rewards = stats.get("rewards") or {}
    if rewards:
        lines.append("Reward distribution:")
        lines.append("")
        lines.extend(
            f"- `reward={reward}`: {count}"
            for reward, count in sorted(rewards.items())
        )
        lines.append("")

    if verdict.passed:
        lines.append(
            "Pipeline healthy. Unsolved trials are a model signal and do not "
            "fail this job."
        )
    else:
        lines.append("### Why this failed")
        lines.append("")
        lines.extend(f"- {reason}" for reason in verdict.reasons)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-path", required=True, type=Path)
    parser.add_argument("--harbor-status", default=0, type=int)
    parser.add_argument(
        "--max-harness-failure-ratio",
        default=DEFAULT_MAX_HARNESS_FAILURE_RATIO,
        type=float,
    )
    parser.add_argument("--step-summary", default=None, type=Path)
    parser.add_argument(
        "--expected-trials",
        default="",
        help="Reject a summary whose trial total differs; empty skips the check",
    )
    args = parser.parse_args(argv)

    try:
        expected_trials = (
            int(args.expected_trials) if args.expected_trials.strip() else None
        )
        try:
            summary_text = (args.output_path / "summary.txt").read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            summary_text = None
        verdict = evaluate(
            summary_text,
            args.max_harness_failure_ratio,
            args.harbor_status,
            expected_trials,
        )
    except (ValueError, OSError) as error:
        print(f"::error::unreadable Harbor summary: {error}", file=sys.stderr)
        return 1

    rendered = render_summary(verdict)
    print(rendered)

    destination = args.step_summary
    if destination is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        destination = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(rendered)

    for reason in verdict.reasons:
        print(f"::error::{reason}", file=sys.stderr)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
