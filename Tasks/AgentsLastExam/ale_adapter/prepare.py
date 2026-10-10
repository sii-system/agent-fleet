"""Explicitly prepare the pinned ALE checkout and its native host dependencies."""

import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from .source import REPOSITORY, REVISION, source_digest, validate_source
from .workflow import digest, native_packages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--env-dir", "--native-env-dir", dest="env_dir", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--lockfile", type=Path, required=True)
    args = parser.parse_args()
    args.source = (args.source or args.cache / "source").resolve()
    args.env_dir = (args.env_dir or args.cache / "native-env").absolute()
    dataset = (args.dataset or args.cache / "tasks").resolve()
    if not args.source.exists():
        args.source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(args.source)], check=True)
        subprocess.run(["git", "-C", str(args.source), "fetch", "--depth", "1", REPOSITORY, REVISION], check=True)
        subprocess.run(["git", "-C", str(args.source), "checkout", "--detach", "FETCH_HEAD"], check=True)
    source = validate_source(args.source)
    if not args.env_dir.exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(args.env_dir)], check=True)
    # Keep native ALE dependencies separate from the pinned Harbor runner.
    # Both requirement sets come from the upstream revision; task eval deps can
    # include large packages such as torch. Startup never installs packages.
    subprocess.run(["uv", "pip", "install", "--torch-backend", "cpu", "--python", str(args.env_dir / "bin/python"),
                    str(source), str(source / "tasks")], check=True)
    python = args.env_dir / "bin/python"
    provenance = {"revision": REVISION, "source_sha256": source_digest(source), "scope": "cpu"}
    if dataset.exists():
        existing = json.loads((dataset / "dataset.json").read_text())
        if any(existing.get(key) != value for key, value in provenance.items()):
            raise ValueError("ALE dataset is stale; choose a new --dataset directory")
    else:
        subprocess.run([str(python), "-m", "ale_adapter.adapter", "--source", str(source),
                        "--output-dir", str(dataset)], check=True)
    packages = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    (Path(sys.prefix) / ".agent-fleet-ale.json").write_text(json.dumps({
        "version": 1, "lock_sha256": digest(args.lockfile), "packages": packages,
        "source": str(source), "dataset": str(dataset), "native_python": str(python),
        "native_packages": native_packages(python),
    }) + "\n")


if __name__ == "__main__":
    main()
