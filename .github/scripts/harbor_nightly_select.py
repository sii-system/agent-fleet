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
REGISTRY_PAGE_SIZE = 1000


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
    """Read CPU-compatible task names from Harbor's package metadata API."""
    try:
        if client_factory is None:
            from harbor.registry.client.package import PackageDatasetClient

            client_factory = PackageDatasetClient
        metadata = await client_factory().get_dataset_metadata(benchmark)
        gpu_refs = await registry_gpu_task_refs(metadata.dataset_version_id)
        eligible = unique_names([
            task_id.get_name() for task_id in metadata.task_ids
            if task_id.ref not in gpu_refs
        ])
        if gpu_refs:
            print(
                f"Skipping {len(gpu_refs)} GPU task(s) from {benchmark}",
                file=sys.stderr,
            )
        return eligible
    except Exception as exc:
        raise RuntimeError(f"unable to read Harbor metadata for {benchmark}") from exc


async def registry_gpu_task_refs(dataset_version_id: str) -> set[str]:
    """Read GPU counts for the same immutable task versions as the dataset."""
    from harbor.auth.client import create_authenticated_client

    if not dataset_version_id:
        raise RuntimeError("Harbor metadata has no dataset version ID")
    client = await create_authenticated_client()
    gpu_refs = set()
    start = 0
    while True:
        response = await (
            client.table("dataset_version_task")
            .select("task_version:task_version_id(content_hash,gpus:environment_config->gpus)")
            .eq("dataset_version_id", dataset_version_id)
            .order("task_version_id")
            .range(start, start + REGISTRY_PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data
        for row in rows:
            version = row["task_version"]
            if (version["gpus"] or 0) > 0:
                gpu_refs.add(f"sha256:{version['content_hash']}")
        if len(rows) < REGISTRY_PAGE_SIZE:
            return gpu_refs
        start += REGISTRY_PAGE_SIZE


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
    if not available:
        raise RuntimeError(f"{benchmark} has no selectable tasks after GPU filtering")
    randomizer = rng or random.SystemRandom()
    return randomizer.sample(available, min(SAMPLE_SIZE, len(available)))


def requested_tasks(repo_root: Path, benchmark: str, tasks: str) -> list[str]:
    selected = unique_names(tasks.split(","))
    if not selected or len(selected) > SAMPLE_SIZE:
        raise ValueError(f"request between 1 and {SAMPLE_SIZE} unique tasks")
    missing = set(selected) - set(task_names(repo_root, benchmark))
    if missing:
        raise ValueError(
            "requested tasks are absent from the selected benchmark or require GPUs: "
            + ",".join(sorted(missing))
        )
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--tasks", default="")
    args = parser.parse_args(argv)
    try:
        selected = (
            requested_tasks(args.repo_root, args.benchmark, args.tasks)
            if args.tasks else select_tasks(args.repo_root, args.benchmark)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(len(selected))
    print(",".join(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
