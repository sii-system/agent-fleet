#!/usr/bin/env python3
"""Validate/cache private benchmark inputs without exposing gold to tasks."""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

from common import (
    BENCHMARK_DIR,
    atomic_write_json,
    default_cache_root,
    default_source_root,
    load_questions,
    sha256_file,
)


def source_commit(source_root: Path) -> str:
    marker = BENCHMARK_DIR / "UPSTREAM_COMMIT"
    if source_root == default_source_root() and marker.is_file():
        return marker.read_text(encoding="utf-8").strip()
    completed = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def download_ground_truth(source_root: Path, output: Path) -> None:
    script = BENCHMARK_DIR / "runtime" / "prepare_dataset.py"
    if not script.is_file():
        raise FileNotFoundError(f"BrowseComp dataset adapter not found: {script}")
    output.parent.mkdir(parents=True, exist_ok=True)
    python = os.environ.get("BROWSECOMP_PYTHON", sys.executable)
    subprocess.run(
        [
            python,
            str(script),
            "--source-root",
            str(source_root),
            "--output",
            str(output),
            "--generate-tsv",
            str(output.parent / "queries.tsv"),
        ],
        check=True,
    )
    output.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=default_source_root())
    parser.add_argument("--cache-root", type=Path, default=default_cache_root())
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--index-path")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    if not (source_root / "searcher" / "mcp_server.py").is_file():
        raise FileNotFoundError(f"invalid BrowseComp-Plus source root: {source_root}")
    cache_root = args.cache_root.expanduser().resolve()
    ground_truth = args.ground_truth
    if ground_truth is None:
        configured = os.environ.get("BROWSECOMP_GROUND_TRUTH")
        ground_truth = Path(configured) if configured else source_root / "data" / "browsecomp_plus_decrypted.jsonl"
    ground_truth = ground_truth.expanduser().resolve()
    if not ground_truth.is_file() and args.download:
        ground_truth = cache_root / "private" / "browsecomp_plus_decrypted.jsonl"
        download_ground_truth(source_root, ground_truth)
    if not ground_truth.is_file():
        raise FileNotFoundError(
            f"ground truth not found: {ground_truth}; run the BrowseComp bootstrap or set BROWSECOMP_GROUND_TRUTH"
        )
    rows = load_questions(ground_truth, require_answer=True)

    index_path = os.path.expanduser(args.index_path or os.environ.get("BROWSECOMP_INDEX_PATH", ""))
    if index_path and not glob.glob(index_path):
        raise FileNotFoundError(f"retrieval index did not match any files: {index_path}")
    manifest = {
        "schema_version": 1,
        "source_root": str(source_root),
        "source_commit": source_commit(source_root),
        "ground_truth_path": str(ground_truth),
        "ground_truth_sha256": sha256_file(ground_truth),
        "query_count": len(rows),
        "index_path": index_path,
    }
    manifest_path = cache_root / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    payload = {**manifest, "manifest_path": str(manifest_path)}
    if args.json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(f"Prepared {len(rows)} BrowseComp-Plus queries")
        print(f"Ground truth (host only): {ground_truth}")
        print(f"Cache manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
