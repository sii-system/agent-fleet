"""Materialize official WAA definitions as native Windows Harbor tasks."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .dataset import BENCHMARKS, digest, load_tasks
from .source import validate_runtime


def task_name(task):
    return f"{task.domain}--{task.id}"


def materialize(runtime, destination, *, benchmark):
    runtime, destination = Path(runtime).resolve(), Path(destination).resolve()
    validate_runtime(runtime, benchmark=benchmark)
    tasks = load_tasks(runtime / "client", benchmark=benchmark)
    provenance = {"version": 1, "benchmark": benchmark, "release": BENCHMARKS[benchmark],
                  "runtime_sha256": digest(runtime / "runtime.json"), "adapter_sha256": digest(__file__)}
    # JSON round-trip normalizes the screen-size tuple.
    provenance = json.loads(json.dumps(provenance))
    if destination.exists():
        manifest = json.loads((destination / "dataset.json").read_text())
        if manifest.get("provenance") != provenance:
            raise ValueError("Harbor WAA dataset is stale; choose a new output directory")
        for name, checksum in manifest["files"].items():
            path = destination / name
            if not path.resolve().is_relative_to(destination) or path.is_symlink() or digest(path) != checksum:
                raise ValueError(f"Harbor task changed: {name}")
        actual = {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file() and p.name != "dataset.json"}
        if actual != set(manifest["files"]):
            raise ValueError("Harbor dataset contains unexpected files")
        return tasks
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".preparing")
    staging.mkdir()
    try:
        for task in tasks:
            root = staging / task_name(task)
            (root / "environment").mkdir(parents=True)
            (root / "tests").mkdir()
            config = json.loads(task.path.read_text())
            (root / "instruction.md").write_text(config["instruction"] + "\n", encoding="utf-8")
            shutil.copyfile(task.path, root / "environment/native-task.json")
            (root / "environment/waa.json").write_text(json.dumps({
                "benchmark": benchmark, "domain": task.domain, "id": task.id,
                "revision": BENCHMARKS[benchmark]["revision"], "task_sha256": task.sha256,
            }, indent=2) + "\n")
            (root / "task.toml").write_text(
                'schema_version = "1.3"\n'
                + f'[metadata]\nbenchmark = "{benchmark}"\ndomain = "{task.domain}"\n'
                + f'native_task_id = "{task.id}"\nupstream_revision = "{BENCHMARKS[benchmark]["revision"]}"\n'
                + '[environment]\nos = "windows"\nnetwork_mode = "public"\n'
                + 'cpus = 4\nmemory_mb = 8192\nbuild_timeout_sec = 3600\n'
                + '[agent]\ntimeout_sec = 3600\n[verifier]\ntimeout_sec = 600\n'
            )
            # Fail closed when a consumer omits the required custom verifier.
            (root / "tests/test.bat").write_bytes(
                b'@echo off\r\necho This task requires waa_benchmark.verifier:WAAVerifier.\r\nexit /b 1\r\n'
            )
        files = {str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        (staging / "dataset.json").write_text(json.dumps({"provenance": provenance, "files": files}, indent=2) + "\n")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    tasks = materialize(args.runtime, args.output_dir, benchmark=args.benchmark)
    print(json.dumps({"benchmark": args.benchmark, "tasks": len(tasks), "dataset": str(args.output_dir)}))


if __name__ == "__main__":
    main()
