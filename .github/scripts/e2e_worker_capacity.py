#!/usr/bin/env python3
"""Derive a Harbor worker count from host CPU and memory capacity.

Terminal-Bench needs 8 cores and 32 GB per worker. Harbor does not enforce
that, so oversubscription is silent: the workers start and then starve. The
bare-metal runners also host code-review container runners, so a reserve is
held back rather than claiming the whole machine.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CORES_PER_WORKER = 8
GB_PER_WORKER = 32
RESERVE_CORES = 16
RESERVE_GB = 64


class CapacityError(Exception):
    """The host cannot fit the requested or minimum worker count."""


def read_available_gb(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // (1024 * 1024)
    raise ValueError("MemAvailable missing from meminfo")


def parse_requested(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"--requested must be a positive integer, got {value!r}")
    return int(text)


def capacity(
    cores: int,
    available_gb: int,
    reserve_cores: int = RESERVE_CORES,
    reserve_gb: int = RESERVE_GB,
) -> int:
    usable_cores = max(0, cores - reserve_cores)
    usable_gb = max(0, available_gb - reserve_gb)
    return min(usable_cores // CORES_PER_WORKER, usable_gb // GB_PER_WORKER)


def resolve(
    cores: int,
    available_gb: int,
    requested: int | None,
) -> tuple[int, str]:
    limit = capacity(cores, available_gb)
    measured = (
        f"cores={cores} available_gb={available_gb} "
        f"reserve_cores={RESERVE_CORES} reserve_gb={RESERVE_GB}"
    )
    if limit < 1:
        raise CapacityError(
            f"host fits no Terminal-Bench worker "
            f"({CORES_PER_WORKER} cores + {GB_PER_WORKER}GB each): {measured}"
        )
    if requested is None:
        return limit, f"workers={limit} derived from capacity ({measured})"
    if requested < 1:
        raise CapacityError(f"requested workers must be >= 1, got {requested}")
    if requested > limit:
        return limit, (
            f"workers={limit}: requested {requested} reduced to capacity ({measured})"
        )
    return requested, f"workers={requested} requested, within capacity ({measured})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requested", default="", help="Requested worker count; empty derives from capacity")
    parser.add_argument("--meminfo", default="/proc/meminfo", type=Path)
    args = parser.parse_args(argv)

    # One try covers input parsing, meminfo reading and sizing so every
    # failure reaches CI as a ::error:: annotation instead of a traceback.
    try:
        requested = parse_requested(args.requested)
        cores = os.cpu_count() or 1
        available_gb = read_available_gb(args.meminfo.read_text(encoding="utf-8"))
        workers, note = resolve(cores, available_gb, requested)
    except (CapacityError, ValueError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    print(note)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"workers={workers}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
