"""Explicitly materialize selected QZ Templates with bounded concurrency."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import qz_template_manager as manager
import qz_template_resolver as resolver

DEFAULT_WORKERS = 8


class QzTemplateBatchError(RuntimeError):
    """Raised when a batch cannot be prepared."""


@dataclass(frozen=True)
class MaterializeJob:
    template_key: str
    entry: Mapping[str, Any]
    tasks: tuple[str, ...]


def _positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be an integer") from exc
    if workers <= 0:
        raise argparse.ArgumentTypeError("workers must be greater than zero")
    return workers


def load_task_names(path: Path) -> list[str]:
    """Load an explicit, non-empty task selection file."""
    path = path.expanduser()
    if not path.is_file():
        raise QzTemplateBatchError(f"task list not found: {path}")
    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise QzTemplateBatchError(f"task list is not valid UTF-8: {path}") from exc
    task_names = []
    for raw_line in contents.splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            task_names.append(value)
    if not task_names:
        raise QzTemplateBatchError(f"task list is empty: {path}")
    return task_names


def plan_jobs(
    payload: Mapping[str, Any],
    task_names: Sequence[str],
) -> list[MaterializeJob]:
    """Validate all selected tasks and deduplicate their Template entries."""
    grouped: dict[str, dict[str, Any]] = {}
    seen_tasks: set[str] = set()
    for task_name in task_names:
        canonical_task = resolver.resolve_task_key(payload, task_name)
        if canonical_task in seen_tasks:
            continue
        seen_tasks.add(canonical_task)
        template_key, entry = resolver.task_template_entry(payload, canonical_task)
        group = grouped.setdefault(
            template_key,
            {"entry": entry, "tasks": []},
        )
        group["tasks"].append(canonical_task)

    return [
        MaterializeJob(
            template_key=template_key,
            entry=group["entry"],
            tasks=tuple(sorted(group["tasks"])),
        )
        for template_key, group in sorted(grouped.items())
    ]


def _run_job(
    job: MaterializeJob,
    client: manager.QzTemplateClient,
    timeout: float,
    stderr: TextIO,
) -> tuple[str | None, str | None]:
    try:
        template_id = resolver.materialize_template_entry(
            job.template_key,
            job.entry,
            client,
            timeout=timeout,
            stderr=stderr,
        )
        return template_id, None
    except (
        OSError,
        manager.QzTemplateError,
        resolver.QzTemplateResolutionError,
    ) as exc:
        return None, str(exc)


def materialize_batch(
    mapping_path: Path,
    task_names: Sequence[str],
    client: manager.QzTemplateClient,
    *,
    workers: int,
    timeout: float,
    stderr: TextIO = sys.stderr,
) -> dict[str, Any]:
    """Materialize unique selected Templates and persist successes serially."""
    if workers <= 0:
        raise QzTemplateBatchError("workers must be greater than zero")

    payload = resolver.load_mapping(mapping_path)
    jobs = plan_jobs(payload, task_names)
    if not jobs:
        raise QzTemplateBatchError("task selection did not produce any jobs")

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
        futures = {
            executor.submit(_run_job, job, client, timeout, stderr): job for job in jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            template_id, error = future.result()
            result = {
                "template_key": job.template_key,
                "tasks": list(job.tasks),
            }
            if error is None and template_id is not None:
                try:
                    resolver.record_template_id(
                        mapping_path,
                        job.template_key,
                        template_id,
                    )
                except (OSError, resolver.QzTemplateResolutionError) as exc:
                    error = f"failed to record ready Template {template_id!r}: {exc}"
                else:
                    result.update(status="ready", template_id=template_id)
                    print(
                        f"ready {job.template_key}: {template_id} "
                        f"({len(job.tasks)} task(s))",
                        file=stderr,
                    )
            if error is not None:
                result.update(status="failed", error=error)
                print(f"failed {job.template_key}: {error}", file=stderr)
            results[job.template_key] = result

    ordered_results = [results[job.template_key] for job in jobs]
    ready_count = sum(result["status"] == "ready" for result in ordered_results)
    return {
        "mapping": str(mapping_path.expanduser().resolve()),
        "selected_task_count": sum(len(job.tasks) for job in jobs),
        "unique_template_count": len(jobs),
        "ready_template_count": ready_count,
        "failed_template_count": len(jobs) - ready_count,
        "templates": ordered_results,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize selected QZ Templates with bounded concurrency.",
    )
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--task-list", required=True, type=Path)
    parser.add_argument("--workers", type=_positive_workers, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--timeout",
        type=manager._positive_timeout,
        default=manager.DEFAULT_BUILD_TIMEOUT_SEC,
        help="per-Template build timeout in seconds",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = parse_args(argv)
    try:
        task_names = load_task_names(args.task_list)
        result = materialize_batch(
            args.mapping,
            task_names,
            manager.client_from_environment(),
            workers=args.workers,
            timeout=args.timeout,
            stderr=stderr,
        )
        json.dump(result, stdout, ensure_ascii=False, indent=2, sort_keys=True)
        stdout.write("\n")
        return 1 if result["failed_template_count"] else 0
    except (
        OSError,
        manager.QzTemplateError,
        QzTemplateBatchError,
        resolver.QzTemplateResolutionError,
    ) as exc:
        print(f"error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
