"""Convert native Linux and Windows CPU variants into Harbor tasks."""

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .source import REVISION, source_digest, validate_source


def discover(source):
    # Exclude GPU snapshots before importing task modules or their dependencies.
    import yaml
    from ale_run.environments.images import get as get_image
    from ale_run.environments.providers.gcloud import _parse_gce_machine_type
    from ale_run.tasks.loader import TaskLoader

    snapshots = yaml.safe_load((source / "configs/environments/environment_gcloud.yaml").read_text())["snapshots"]
    records = []
    for card in sorted((source / "tasks").glob("*/*/task_card.json")):
        definition = json.loads(card.read_text())
        snapshot = definition["vm"]["snapshot"]
        profile = snapshots[snapshot]
        if profile.get("gcloud", {}).get("gpu"):
            continue
        os_type = get_image(profile["image"]).os
        loader = TaskLoader(str(card.parent))
        # A load() list is ALE's authoritative variant enumeration.
        module = loader._load_module()
        variants = module.load() if callable(getattr(module, "load", None)) else [None]
        if not variants or not callable(loader.get_evaluate_fn()):
            raise ValueError(f"Missing ALE variants or evaluator: {card.parent}")
        for index in range(len(variants)):
            info = loader.load(variant_index=index)
            if info["os_type"] != os_type or not info["description"].strip():
                raise ValueError(f"Invalid ALE task OS or description: {card.parent}, variant {index}")
            shape = _parse_gce_machine_type(info.get("machine_type"))
            records.append({
                "task": str(card.parent.relative_to(source)), "variant": index,
                "description": info["description"], "snapshot": snapshot, "os": os_type,
                "image_family": profile["image"], "resolution": profile.get("resolution"),
                "requires_gpu": False,
                "cpus": info.get("vcpus") or (shape.vcpus if shape else 4),
                "memory_mb": 1024 * (info.get("memory_gb") or (shape.memory_gb if shape else 16)),
                "timeout": info.get("timeout_s", 7200),
            })
    if not records:
        raise ValueError("No CPU ALE tasks found")
    return records


def materialize(source, output):
    source, output = validate_source(source), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new output directory for ALE conversion")
    sys.path.insert(0, str(source))
    # Native tasks can print; reserve stdout for the conversion summary.
    with contextlib.redirect_stdout(sys.stderr):
        records = discover(source)
    provenance = {"revision": REVISION, "source_sha256": source_digest(source), "scope": "cpu"}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + "-", dir=output.parent))
    try:
        for record in records:
            name = "--".join(Path(record["task"]).parts[1:]) + f"--v{record['variant']}"
            root = staging / name
            (root / "environment").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "instruction.md").write_text(record["description"] + "\n", encoding="utf-8")
            (root / "environment/ale.json").write_text(json.dumps({**provenance, **record}, indent=2) + "\n")
            (root / "task.toml").write_text(
                'schema_version = "1.3"\n[metadata]\nbenchmark = "ale-cpu"\n'
                f'upstream_revision = "{REVISION}"\n'
                f'[environment]\nos = "{record["os"]}"\nnetwork_mode = "public"\n'
                f'gpus = {int(record["requires_gpu"])}\n'
                f'cpus = {record["cpus"]}\nmemory_mb = {record["memory_mb"]}\nbuild_timeout_sec = 3600\n'
                f'[agent]\ntimeout_sec = {record["timeout"]}\n[verifier]\ntimeout_sec = 3600\n',
            )
            if record["os"] == "windows":
                (root / "tests/test.bat").write_bytes(
                    b'@echo off\r\necho Requires ale_adapter.verifier:ALEVerifier.\r\nexit /b 1\r\n'
                )
            else:
                (root / "tests/test.sh").write_text(
                    '#!/usr/bin/env bash\nset -euo pipefail\necho "Requires ale_adapter.verifier:ALEVerifier." >&2\nexit 1\n'
                )
        (staging / "dataset.json").write_text(json.dumps({**provenance, "tasks": len(records)}, indent=2) + "\n")
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return len(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"tasks": materialize(args.source, args.output_dir), "dataset": str(args.output_dir)}))


if __name__ == "__main__":
    main()
