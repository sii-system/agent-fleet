"""Shared reproducibility helpers for isolated Fusion Router wrappers."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Protocol

WHEEL_METADATA_KEYS = ("cache_dir", "source_hash", "wheel", "wheel_sha256")


def wheel_metadata_values(raw: str) -> list[str]:
    payload = json.loads(raw)
    return [str(payload[key]) for key in WHEEL_METADATA_KEYS]


def extract_wheel(wheel: Path, destination: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(destination)


def validate_doctor(raw: str, pipeline: str) -> None:
    payload = json.loads(raw)
    if payload.get("status") != "ok" or payload.get("pipeline") != pipeline:
        raise RuntimeError(f"Router doctor failed: {payload}")


def task_list_csv(path: Path) -> str:
    tasks = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    tasks = [task for task in tasks if task and not task.startswith("#")]
    if not tasks or any("," in task for task in tasks):
        raise ValueError("task list must contain nonempty task IDs without commas")
    return ",".join(tasks)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _update_digest_from_file(digest: _Digest, path: Path) -> None:
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)


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
    index_output = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "-z", "--cached"],
        check=True,
        capture_output=True,
    ).stdout
    index_entries: dict[bytes, tuple[bytes, bytes]] = {}
    for entry in (item for item in index_output.split(b"\0") if item):
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split()
        if separator and len(fields) >= 2:
            index_entries[raw_path] = (fields[0], fields[1])

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
        elif stat.S_ISDIR(mode):
            index_mode, index_object = index_entries.get(raw_path, (b"", b""))
            digest.update(b"directory\0")
            digest.update(index_mode)
            digest.update(b"\0")
            digest.update(index_object)
            digest.update(b"\0")
            if index_mode == b"160000":
                submodule_root = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                    check=False,
                    capture_output=True,
                )
                is_populated_submodule = (
                    submodule_root.returncode == 0
                    and Path(os.fsdecode(submodule_root.stdout.strip())).resolve()
                    == path.resolve()
                )
                if is_populated_submodule:
                    submodule_head = subprocess.run(
                        ["git", "-C", str(path), "rev-parse", "HEAD"],
                        check=True,
                        capture_output=True,
                    )
                    digest.update(submodule_head.stdout.strip())
                    digest.update(b"\0")
                    digest.update(source_fingerprint(path).encode("ascii"))
        else:
            _update_digest_from_file(digest, path)
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_publish_bytes(target: Path, content: bytes, mode: int) -> None:
    """Publish immutable content only after the complete file is durable."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
            _fsync_directory(target.parent)
        except FileExistsError:
            if target.read_bytes() != content:
                raise RuntimeError(
                    f"content-addressed artifact mismatch: {target}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def build_wheel(repo: Path, cache_root: Path, version: str) -> dict[str, str]:
    """Build and atomically cache a wheel for a stable source fingerprint."""
    repo = repo.resolve(strict=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root = cache_root.resolve(strict=True)

    for _ in range(3):
        fingerprint = source_fingerprint(repo)
        cache_dir = cache_root / f"{version}-{fingerprint[:12]}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_root / f".{fingerprint}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if source_fingerprint(repo) != fingerprint:
                continue

            checksum_files = list(cache_dir.glob("*.whl.sha256"))
            wheels = list(cache_dir.glob("*.whl"))
            if len(wheels) == 1 and len(checksum_files) == 1:
                wheel = wheels[0]
                expected = checksum_files[0].read_text(encoding="ascii").strip()
                actual = _sha256(wheel)
                if (
                    checksum_files[0]
                    == wheel.with_suffix(wheel.suffix + ".sha256")
                    and expected == actual
                ):
                    return {
                        "cache_dir": str(cache_dir),
                        "source_hash": fingerprint,
                        "wheel": str(wheel),
                        "wheel_sha256": actual,
                    }

            for stale in (*wheels, *checksum_files):
                stale.unlink(missing_ok=True)

            with tempfile.TemporaryDirectory(
                prefix=".router-build-", dir=cache_root
            ) as temporary_name:
                temporary = Path(temporary_name)
                subprocess.run(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--out-dir",
                        str(temporary),
                        str(repo),
                    ],
                    check=True,
                    stdout=sys.stderr,
                )
                built_wheels = list(temporary.glob("*.whl"))
                if len(built_wheels) != 1:
                    raise RuntimeError(
                        f"expected one Router wheel, found {len(built_wheels)}"
                    )
                if source_fingerprint(repo) != fingerprint:
                    continue

                built = built_wheels[0]
                wheel = cache_dir / built.name
                checksum = _sha256(built)
                os.replace(built, wheel)
                with wheel.open("rb") as handle:
                    os.fsync(handle.fileno())
                _atomic_publish_bytes(
                    wheel.with_suffix(wheel.suffix + ".sha256"),
                    f"{checksum}\n".encode("ascii"),
                    0o444,
                )
                _fsync_directory(cache_dir)
                return {
                    "cache_dir": str(cache_dir),
                    "source_hash": fingerprint,
                    "wheel": str(wheel),
                    "wheel_sha256": checksum,
                }

    raise RuntimeError("Router source changed during three consecutive wheel builds")


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
    _atomic_publish_bytes(target, content, 0o444)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fingerprint_parser = subparsers.add_parser("source-fingerprint")
    fingerprint_parser.add_argument("repo", type=Path)
    wheel_parser = subparsers.add_parser("build-wheel")
    wheel_parser.add_argument("--repo", required=True, type=Path)
    wheel_parser.add_argument("--cache-root", required=True, type=Path)
    wheel_parser.add_argument("--version", required=True)
    config_parser = subparsers.add_parser("derive-config")
    config_parser.add_argument("--source", required=True, type=Path)
    config_parser.add_argument("--output-dir", required=True, type=Path)
    config_parser.add_argument(
        "--pipeline", required=True, choices=("mimo_max", "openrouter_fusion")
    )
    config_parser.add_argument("--max-fusions", required=True, type=int)
    metadata_parser = subparsers.add_parser("wheel-metadata-values")
    metadata_parser.add_argument("metadata")
    extract_parser = subparsers.add_parser("extract-wheel")
    extract_parser.add_argument("wheel", type=Path)
    extract_parser.add_argument("destination", type=Path)
    doctor_parser = subparsers.add_parser("validate-doctor")
    doctor_parser.add_argument("payload")
    doctor_parser.add_argument("pipeline")
    tasks_parser = subparsers.add_parser("task-list-csv")
    tasks_parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "source-fingerprint":
        print(source_fingerprint(args.repo))
    elif args.command == "build-wheel":
        print(json.dumps(build_wheel(args.repo, args.cache_root, args.version)))
    elif args.command == "derive-config":
        print(
            derive_config(
                args.source, args.output_dir, args.pipeline, args.max_fusions
            )
        )
    elif args.command == "wheel-metadata-values":
        print("\n".join(wheel_metadata_values(args.metadata)))
    elif args.command == "extract-wheel":
        extract_wheel(args.wheel, args.destination)
    elif args.command == "validate-doctor":
        validate_doctor(args.payload, args.pipeline)
    else:
        print(task_list_csv(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
