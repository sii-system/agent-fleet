"""Fetch only the encrypted question columns and decrypt them on the host."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import ModuleType

DATASET_REPO = "Tevatron/browsecomp-plus"
# Pin data independently from the source-code submodule. This is the revision
# whose card declares 830 test examples and is compatible with the source pin.
DATASET_REVISION = "144cff8e35b5eaef7e526346aa60774a9deb941f"
REQUIRED_COLUMNS = ("query_id", "query", "answer")
EXPECTED_ROWS = 830


def load_official_decrypt(source_root: Path) -> ModuleType:
    """Load the canary and decryptor from the unchanged upstream snapshot."""

    script = source_root / "scripts_build_index" / "decrypt_dataset.py"
    if not script.is_file():
        raise FileNotFoundError(f"vendored decrypt script is missing: {script}")
    spec = importlib.util.spec_from_file_location("browsecomp_upstream_decrypt", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load upstream decrypt module: {script}")
    module = importlib.util.module_from_spec(spec)
    # The vendored checkout is read-only by policy. Dynamic imports normally
    # leave __pycache__ beside the source even though no source file changed.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def list_shards() -> list[str]:
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(
        DATASET_REPO,
        repo_type="dataset",
        revision=DATASET_REVISION,
    )
    shards = sorted(
        name
        for name in files
        if name.startswith("data/test-") and name.endswith(".parquet")
    )
    if not shards:
        raise FileNotFoundError(
            f"no test parquet shards found in {DATASET_REPO}@{DATASET_REVISION}"
        )
    return shards


def read_projected_shard(name: str) -> list[dict[str, object]]:
    """Read only query columns through HTTP ranges, not the multi-GB doc columns."""

    from huggingface_hub import HfFileSystem
    from pyarrow import parquet

    path = f"datasets/{DATASET_REPO}@{DATASET_REVISION}/{name}"
    table = parquet.read_table(
        path,
        filesystem=HfFileSystem(),
        columns=list(REQUIRED_COLUMNS),
        pre_buffer=True,
        use_threads=True,
    )
    return table.to_pylist()


def fetch_projected_rows(
    shards: list[str],
    reader: Callable[[str], list[dict[str, object]]] = read_projected_shard,
) -> list[dict[str, object]]:
    workers = min(len(shards), max(1, int(os.environ.get("BROWSECOMP_DATA_WORKERS", "6"))))
    by_name: dict[str, list[dict[str, object]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(reader, name): name for name in shards}
        for future in as_completed(futures):
            name = futures[future]
            rows = future.result()
            by_name[name] = rows
            print(f"[BrowseComp] question shard ready: {name} ({len(rows)} rows)", flush=True)
    return [row for name in sorted(shards) for row in by_name[name]]


def write_private_dataset(
    rows: list[dict[str, object]],
    output: Path,
    tsv_output: Path | None,
    decrypt_string: Callable[[str, str], str],
    canary: str,
) -> None:
    if len(rows) != EXPECTED_ROWS:
        raise ValueError(
            f"pinned BrowseComp dataset returned {len(rows)} rows; expected {EXPECTED_ROWS}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if tsv_output:
        tsv_output.parent.mkdir(parents=True, exist_ok=True)

    json_fd, json_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    tsv_fd: int | None = None
    tsv_name: str | None = None
    if tsv_output:
        tsv_fd, tsv_name = tempfile.mkstemp(
            prefix=f".{tsv_output.name}.", dir=tsv_output.parent
        )
    try:
        os.chmod(json_name, 0o600)
        if tsv_name:
            os.chmod(tsv_name, 0o600)
        with os.fdopen(json_fd, "w", encoding="utf-8") as jsonl_handle:
            tsv_handle = (
                os.fdopen(tsv_fd, "w", encoding="utf-8")
                if tsv_fd is not None
                else None
            )
            try:
                seen: set[str] = set()
                for position, row in enumerate(rows, 1):
                    query_id = str(row.get("query_id", "")).strip()
                    encrypted_query = row.get("query")
                    encrypted_answer = row.get("answer")
                    if not query_id or query_id in seen:
                        raise ValueError(
                            f"invalid or duplicate query_id at dataset row {position}: {query_id!r}"
                        )
                    if not isinstance(encrypted_query, str) or not isinstance(
                        encrypted_answer, str
                    ):
                        raise TypeError(
                            f"query and answer must be encrypted strings at dataset row {position}"
                        )
                    seen.add(query_id)
                    query = decrypt_string(encrypted_query, canary)
                    answer = decrypt_string(encrypted_answer, canary)
                    json.dump(
                        {"query_id": query_id, "query": query, "answer": answer},
                        jsonl_handle,
                        ensure_ascii=False,
                    )
                    jsonl_handle.write("\n")
                    if tsv_handle:
                        tsv_handle.write(
                            f"{query_id}\t{query.replace(chr(9), ' ').replace(chr(10), ' ')}\n"
                        )
            finally:
                if tsv_handle:
                    tsv_handle.close()
        Path(json_name).replace(output)
        if tsv_output and tsv_name:
            Path(tsv_name).replace(tsv_output)
    finally:
        Path(json_name).unlink(missing_ok=True)
        if tsv_name:
            Path(tsv_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--generate-tsv", type=Path)
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    tsv_output = args.generate_tsv.expanduser().resolve() if args.generate_tsv else None
    official = load_official_decrypt(source_root)
    print(
        "[BrowseComp] reading query_id/query/answer with Parquet column projection "
        "(large document columns are skipped)",
        flush=True,
    )
    rows = fetch_projected_rows(list_shards())
    write_private_dataset(
        rows,
        output,
        tsv_output,
        official.decrypt_string,
        official.DEFAULT_CANARY,
    )
    print(f"[BrowseComp] decrypted {len(rows)} questions to private cache: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
