#!/usr/bin/env python3
"""Select a random task sample for one Harbor benchmark."""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOCAL_TASK_LISTS = {
    "seta": Path("Tasks/SETA/harbor_tasks.txt"),
    "smith": Path("Tasks/SWE-smith/harbor_tasks.txt"),
    "sweverify": Path("Tasks/SWE-verify/harbor_tasks.txt"),
}
SAMPLE_SIZE = 20


def unique_names(names: list[str]) -> list[str]:
    unique = list(
        dict.fromkeys(
            name.strip()
            for name in names
            if name.strip() and not name.lstrip().startswith("#")
        )
    )
    if any("," in name for name in unique):
        raise RuntimeError("task names containing commas are unsupported")
    return unique


def local_task_names(repo_root: Path, benchmark: str) -> list[str] | None:
    relative_path = LOCAL_TASK_LISTS.get(benchmark)
    if relative_path is None:
        return None
    path = repo_root / relative_path
    if not path.is_file():
        raise RuntimeError(f"task list does not exist: {path}")
    return unique_names(path.read_text(encoding="utf-8").splitlines())


async def _registry_task_names(
    benchmark: str, client_factory: Callable[[], Any] | None = None
) -> list[str]:
    """Read task names from Harbor's package metadata API."""
    try:
        if client_factory is None:
            from harbor.registry.client.package import PackageDatasetClient

            client_factory = PackageDatasetClient
        metadata = await client_factory().get_dataset_metadata(benchmark)
        return unique_names([task_id.get_name() for task_id in metadata.task_ids])
    except Exception as exc:
        raise RuntimeError(f"unable to read Harbor metadata for {benchmark}") from exc


def registry_task_names(
    benchmark: str, client_factory: Callable[[], Any] | None = None
) -> list[str]:
    return asyncio.run(_registry_task_names(benchmark, client_factory))


def task_names(repo_root: Path, benchmark: str) -> list[str]:
    local_names = local_task_names(repo_root, benchmark)
    return local_names if local_names is not None else registry_task_names(benchmark)


def select_tasks(
    repo_root: Path, benchmark: str, rng: Any | None = None
) -> list[str]:
    available = task_names(repo_root, benchmark)
    if len(available) < SAMPLE_SIZE:
        raise RuntimeError(
            f"{benchmark} has only {len(available)} selectable tasks; need {SAMPLE_SIZE}"
        )
    randomizer = rng or random.SystemRandom()
    return randomizer.sample(available, SAMPLE_SIZE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--benchmark", required=True)
    args = parser.parse_args(argv)
    try:
        selected = select_tasks(args.repo_root, args.benchmark)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(len(selected))
    print(",".join(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
