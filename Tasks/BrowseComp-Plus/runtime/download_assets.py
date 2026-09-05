#!/usr/bin/env python3
"""Download one published BrowseComp-Plus index into Agent Fleet's cache."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from huggingface_hub import snapshot_download

INDEX_REPO = "Tevatron/browsecomp-plus-indexes"
INDEX_REVISION = "b3f37f70c33829eb09d04784a54277a31871fd63"
COMPLETE_MARKER = ".agent-fleet-complete.json"


def write_completion_marker(variant_root: Path, variant: str) -> None:
    shards = sorted(variant_root.glob("corpus.shard*.pkl"))
    if not shards or any(path.stat().st_size <= 0 for path in shards):
        raise FileNotFoundError(f"downloaded index is incomplete: {variant_root}")
    payload = {
        "schema_version": 1,
        "repo": INDEX_REPO,
        "revision": INDEX_REVISION,
        "variant": variant,
        "files": [
            {"name": path.name, "size": path.stat().st_size} for path in shards
        ],
    }
    fd, temporary = tempfile.mkstemp(prefix=".complete.", dir=variant_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temporary).replace(variant_root / COMPLETE_MARKER)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=INDEX_REPO,
        repo_type="dataset",
        revision=INDEX_REVISION,
        allow_patterns=[f"{args.variant}/*"],
        local_dir=output_root,
        local_files_only=args.offline,
    )
    write_completion_marker(output_root / args.variant, args.variant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
