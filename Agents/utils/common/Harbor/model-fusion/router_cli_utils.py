"""Shared reproducibility helpers for isolated Fusion Router wrappers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def source_fingerprint(repo: Path) -> str:
    """Hash all tracked and non-ignored untracked source content."""
    output = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    ).stdout
    paths = sorted(item for item in output.split(b"\0") if item)
    digest = hashlib.sha256()
    for raw_path in paths:
        relative = os.fsdecode(raw_path)
        path = repo / relative
        digest.update(raw_path)
        digest.update(b"\0")
        if not path.exists() and not path.is_symlink():
            digest.update(b"missing\0")
            continue
        mode = path.lstat().st_mode
        digest.update(f"{stat.S_IFMT(mode):o}:{stat.S_IMODE(mode):o}".encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def derive_config(
    source: Path, output_dir: Path, pipeline: str, max_fusions: int
) -> Path:
    """Create or reuse an immutable, content-addressed Router config."""
    if max_fusions < -1:
        raise ValueError("max_fusions must be -1 or greater")
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload.setdefault("routing", {})["max_fusions"] = max_fusions
    models = payload.setdefault("models", {})
    models.update(
        {
            "panels": ["sonnet", "sonnet"],
            "reviewer": "sonnet",
            "outer": "sonnet",
            "spec_checklist": "sonnet",
        }
    )
    if pipeline == "mimo_max":
        payload.setdefault("mimo_max", {}).update(
            {
                "model": "sonnet",
                "selector_model": "sonnet",
                "verifier_model": "sonnet",
            }
        )
    elif pipeline != "openrouter_fusion":
        raise ValueError(f"unsupported pipeline: {pipeline}")

    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    fingerprint = hashlib.sha256(content).hexdigest()[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"router-config-{pipeline}-{fingerprint}.json"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if target.read_bytes() != content:
            raise RuntimeError(f"content-addressed config mismatch: {target}") from None
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fingerprint_parser = subparsers.add_parser("source-fingerprint")
    fingerprint_parser.add_argument("repo", type=Path)
    config_parser = subparsers.add_parser("derive-config")
    config_parser.add_argument("--source", required=True, type=Path)
    config_parser.add_argument("--output-dir", required=True, type=Path)
    config_parser.add_argument(
        "--pipeline", required=True, choices=("mimo_max", "openrouter_fusion")
    )
    config_parser.add_argument("--max-fusions", required=True, type=int)
    args = parser.parse_args()
    if args.command == "source-fingerprint":
        print(source_fingerprint(args.repo))
    else:
        print(
            derive_config(
                args.source, args.output_dir, args.pipeline, args.max_fusions
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
