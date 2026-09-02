from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import WebResearchAdapter


def _run(benchmark: str) -> None:
    parser = argparse.ArgumentParser(description=f"Generate Harbor {benchmark} tasks")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    adapter = WebResearchAdapter(
        benchmark,
        args.input,
        args.output_dir,
        image=args.image,
        overwrite=args.overwrite,
    )
    generated = adapter.run(args.task_ids, args.limit)
    print(f"generated {len(generated)} {benchmark} tasks in {args.output_dir}")


def browsecomp_main() -> None:
    _run("browsecomp")


def deepsearchqa_main() -> None:
    _run("deepsearchqa")
