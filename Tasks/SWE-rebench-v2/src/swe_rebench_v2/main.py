from __future__ import annotations

import argparse
import json
from pathlib import Path

from swe_rebench_v2.adapter import SWERebenchV2Adapter
from swe_rebench_v2.loader import LocalDatasetLoader

DEFAULT_DATASET_PATH = Path("/data/harbor-datasets/SWE-rebench-V2")
DEFAULT_DATASET_SOURCE = "nebius/SWE-rebench-V2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert local SWE-rebench-V2 records to Harbor tasks"
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--instance-id", help="Convert one instance")
    selection.add_argument("--task-ids", nargs="+", help="Convert selected instances")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Convert all matching records; required explicitly for full conversion",
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Local dataset directory, Parquet file, or JSON file (default: {DEFAULT_DATASET_PATH})",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset-source",
        default=DEFAULT_DATASET_SOURCE,
        help=(
            "Stable dataset identifier stored in generated metadata "
            f"(default: {DEFAULT_DATASET_SOURCE})"
        ),
    )
    parser.add_argument(
        "--language",
        action="append",
        help="Filter --all by language; repeat or use comma-separated values",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=3000.0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--summary-json", type=Path)
    return parser


def _languages(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    normalized = {
        item.strip().lower()
        for value in values
        for item in value.split(",")
        if item.strip()
    }
    return normalized or None


def run(args: argparse.Namespace) -> int:
    loader = LocalDatasetLoader(args.dataset_path)
    instance_ids = None
    if args.instance_id:
        instance_ids = [args.instance_id]
    elif args.task_ids:
        instance_ids = args.task_ids

    records = loader.select(
        instance_ids=instance_ids,
        languages=_languages(args.language),
        limit=args.limit,
    )
    adapter = SWERebenchV2Adapter(
        args.output_dir,
        source=args.dataset_source,
        max_timeout_sec=args.timeout,
    )
    summary = adapter.generate_many(records, overwrite=args.overwrite)
    serialized = json.dumps(summary.as_dict(), indent=2, ensure_ascii=False) + "\n"
    print(serialized, end="")

    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(serialized, encoding="utf-8")
    return 1 if summary.failures else 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = run(args)
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
