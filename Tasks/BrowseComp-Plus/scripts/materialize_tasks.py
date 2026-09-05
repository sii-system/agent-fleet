#!/usr/bin/env python3
"""Render gold-free BrowseComp questions as ordinary local Harbor tasks."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from common import (
    BENCHMARK_DIR,
    atomic_write_json,
    load_questions,
    parse_selection,
    sha256_text,
    write_lines,
)


def materialize(
    ground_truth: Path,
    output_root: Path,
    selection: list[str],
    task_file: Path,
    manifest_path: Path,
    limit: int = 0,
    allowed_hosts: list[str] | None = None,
    existing_task_file: Path | None = None,
) -> list[str]:
    rows = load_questions(ground_truth, require_answer=True)
    by_id = {str(row["query_id"]): row for row in rows}
    selected = selection or list(by_id)
    if limit > 0 and not selection:
        selected = selected[:limit]
    missing = [task_id for task_id in selected if task_id not in by_id]
    if missing:
        raise ValueError(f"unknown BrowseComp query ids: {', '.join(missing)}")
    if (
        existing_task_file is not None
        and existing_task_file.is_file()
        and existing_task_file.stat().st_size > 0
    ):
        existing = [
            line.strip()
            for line in existing_task_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if existing != selected:
            raise ValueError(
                "task selection does not match existing Harbor task file: "
                f"{existing_task_file}; set RESET_RUN=1 or use a new RUN_ID"
            )

    template_root = BENCHMARK_DIR / "templates"
    instruction_template = (template_root / "instruction.md.template").read_text(encoding="utf-8")
    normalized_allowed_hosts = list(
        dict.fromkeys(host.strip() for host in (allowed_hosts or []) if host.strip())
    )
    task_template = (template_root / "task.toml").read_text(encoding="utf-8").replace(
        "{{ALLOWED_HOSTS}}",
        json.dumps(normalized_allowed_hosts, ensure_ascii=True),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_tasks = []
    for task_id in selected:
        row = by_id[task_id]
        query = str(row["query"])
        task_root = output_root / task_id
        (task_root / "environment").mkdir(parents=True, exist_ok=True)
        (task_root / "tests").mkdir(parents=True, exist_ok=True)
        instruction = instruction_template.replace("{{QUERY_ID}}", task_id).replace("{{QUERY}}", query)
        (task_root / "instruction.md").write_text(instruction, encoding="utf-8")
        (task_root / "task.toml").write_text(task_template, encoding="utf-8")
        shutil.copyfile(template_root / "environment" / "Dockerfile", task_root / "environment" / "Dockerfile")
        verifier = task_root / "tests" / "test.sh"
        shutil.copyfile(template_root / "tests" / "test.sh", verifier)
        verifier.chmod(0o755)
        manifest_tasks.append({"query_id": task_id, "query_sha256": sha256_text(query)})

    write_lines(task_file, selected)
    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "dataset_root": str(output_root.resolve()),
            "task_file": str(task_file.resolve()),
            "task_count": len(selected),
            "tasks": manifest_tasks,
            "contains_gold": False,
            "allowed_hosts": normalized_allowed_hosts,
        },
    )
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--tasks", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--existing-task-file", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    selected = materialize(
        args.ground_truth.resolve(),
        args.output_root.resolve(),
        parse_selection(args.tasks),
        args.task_file.resolve(),
        args.manifest.resolve(),
        limit=args.limit,
        allowed_hosts=args.allowed_host,
        existing_task_file=(
            args.existing_task_file.resolve() if args.existing_task_file else None
        ),
    )
    if args.json:
        print(json.dumps({"task_count": len(selected), "task_ids": selected}, separators=(",", ":")))
    else:
        print(f"Materialized {len(selected)} gold-free Harbor tasks at {args.output_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
