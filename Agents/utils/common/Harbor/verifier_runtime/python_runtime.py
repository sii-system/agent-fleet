"""Build and validate the common static Python verifier-runtime primitive.

This module is task-agnostic: it does not know a benchmark, parser, verifier
entrypoint, or bundle ID. It deliberately does not reuse the host interpreter.
Instead, it downloads a content-addressed python-build-standalone archive,
validates its ELF linkage, and packages a wrapper that clears task-image
library overrides. Task-specific child packages compose this archive into a
complete verifier bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

RUNTIME_ROOT = "python3.12-runtime"
STATIC_MANIFEST = "static-runtime.json"
STATIC_SOURCE = {
    "schema_version": 1,
    "python_version": "3.12.14",
    "release": "20260901",
    "target": "x86_64-unknown-linux-musl",
    "linkage": "static",
    "url": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20260901/cpython-3.12.14%2B20260901-x86_64-unknown-linux-musl-"
        "lto%2Bstatic-full.tar.zst"
    ),
    "sha256": "2b89d2b76515cd89007f6b7c7ef7a5b0815aa2ff871a1572324374b1a063f52a",
}
STATIC_WRAPPER = '''#!/usr/bin/env bash
set -euo pipefail
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) ;;
  *) echo 'verifier runtime error: unsupported_arch (requires Linux x86_64)' >&2; exit 126 ;;
esac
SELF_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
export PYTHONHOME="$(cd "$SELF_DIR/.." && pwd)"
unset PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$SELF_DIR/python3.12.real" -s "$@"
'''


def static_manifest() -> dict[str, object]:
    """Return the manifest embedded in every generated static runtime."""
    return {
        **STATIC_SOURCE,
        "wrapper_sha256": hashlib.sha256(STATIC_WRAPPER.encode()).hexdigest(),
    }


def _sha256_file(path: Path) -> str:
    """Compute a streaming SHA-256 digest without loading the archive in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_static_elf(binary: bytes) -> None:
    """Reject an ELF that depends on the task image loader or shared libraries.

    Args:
        binary: Complete bytes of the candidate Python executable.

    Raises:
        ValueError: If the file is not a 64-bit x86_64 ELF or has dynamic
            program headers.
    """
    if len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01":
        raise ValueError("static runtime requires a 64-bit little-endian ELF")
    if struct.unpack_from("<H", binary, 18)[0] != 62:
        raise ValueError("static runtime requires x86_64")
    offset = struct.unpack_from("<Q", binary, 32)[0]
    size, count = struct.unpack_from("<HH", binary, 54)
    if size < 56 or not count or offset + size * count > len(binary):
        raise ValueError("invalid ELF program headers")
    for index in range(count):
        program_type = struct.unpack_from("<I", binary, offset + size * index)[0]
        if program_type in (2, 3):
            raise ValueError("runtime is not static: PT_DYNAMIC or PT_INTERP found")


def static_archive_ready(path: Path, root: str = RUNTIME_ROOT) -> bool:
    """Return whether an archive satisfies the static-runtime contract.

    The optional root argument lets the bundle preparer validate the same
    runtime after it has been renamed to the bundle's top-level directory.
    """
    try:
        with tarfile.open(path) as archive:
            manifest_file = archive.extractfile(f"{root}/{STATIC_MANIFEST}")
            wrapper_file = archive.extractfile(f"{root}/bin/python3.12")
            if manifest_file is None or json.load(manifest_file) != static_manifest():
                return False
            if wrapper_file is None or wrapper_file.read() != STATIC_WRAPPER.encode():
                return False
            for name in ("bin/python3.12", "bin/python3.12.real"):
                member = archive.getmember(f"{root}/{name}")
                if not member.isfile() or not member.mode & 0o111:
                    return False
            for name in (
                "encodings/__init__.py",
                "json/__init__.py",
                "xml/etree/ElementTree.py",
            ):
                if not archive.getmember(f"{root}/lib/python3.12/{name}").isfile():
                    return False
            binary = archive.extractfile(f"{root}/bin/python3.12.real")
            if binary is None:
                return False
            validate_static_elf(binary.read())
        return True
    except (OSError, KeyError, ValueError, EOFError, struct.error, tarfile.TarError):
        return False


def _static_source_archive(cache_dir: Path) -> Path:
    """Resolve the pinned source from an override, cache, or Cache Gateway.

    The returned path has already passed the pinned SHA-256 check.  No source
    is accepted merely because its filename resembles the expected asset.
    """
    override = os.environ.get("HARBOR_VERIFIER_PYTHON_SOURCE_ARCHIVE")
    source = (
        Path(override)
        if override
        else cache_dir / "cpython-verifier-static.tar.zst"
    )
    if source.is_file():
        if _sha256_file(source) == STATIC_SOURCE["sha256"]:
            return source
        raise RuntimeError("verifier runtime error: source_checksum_mismatch")
    if override:
        raise RuntimeError("verifier Python source archive does not exist")
    gateway = os.environ.get("ARTIFACT_CACHE_GATEWAY_URL", "").rstrip("/")
    if not gateway:
        raise RuntimeError(
            "Set ARTIFACT_CACHE_GATEWAY_URL or HARBOR_VERIFIER_PYTHON_SOURCE_ARCHIVE; "
            "verifier Python must come from the pinned standalone archive"
        )
    parsed = urlsplit(gateway)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ARTIFACT_CACHE_GATEWAY_URL must be credential-free HTTP(S)")
    upstream = urlsplit(STATIC_SOURCE["url"])
    url = (
        f"{gateway}/download/v1/{upstream.scheme}/{upstream.netloc.encode().hex()}"
        f"/object/{upstream.path.encode().hex()}"
    )
    opener = build_opener(ProxyHandler({}))
    fd, name = tempfile.mkstemp(prefix=".verifier-source-", dir=cache_dir)
    os.close(fd)
    temporary = Path(name)
    try:
        print("[prepare] downloading pinned static verifier Python via Gateway", flush=True)
        with opener.open(url, timeout=180) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if _sha256_file(temporary) != STATIC_SOURCE["sha256"]:
            raise RuntimeError("verifier runtime error: source_checksum_mismatch")
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)
    return source


def build_static(target: Path) -> None:
    """Build an atomically replaced static-runtime tarball at target.

    The standalone archive is extracted with bsdtar because the source is
    zstd-compressed.  The resulting executable is renamed behind a wrapper so
    callers always enter through the environment-sanitizing shell contract.
    """
    if static_archive_ready(target):
        print("[prepare] skip static verifier Python (cached)")
        return
    if not shutil.which("bsdtar"):
        raise RuntimeError(
            "static verifier preparation requires bsdtar with zstd support on the runner"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _static_source_archive(target.parent)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".static-python-") as name:
        temporary = Path(name)
        subprocess.run(
            [
                "bsdtar",
                "-xf",
                str(source.resolve()),
                "-C",
                str(temporary),
                "python/install",
            ],
            check=True,
            timeout=180,
        )
        root = temporary / RUNTIME_ROOT
        (temporary / "python" / "install").rename(root)
        binary = root / "bin" / "python3.12"
        validate_static_elf(binary.read_bytes())
        binary.rename(binary.with_name("python3.12.real"))
        binary.write_text(STATIC_WRAPPER, encoding="utf-8")
        binary.chmod(0o755)
        (root / STATIC_MANIFEST).write_text(
            json.dumps(static_manifest(), indent=2) + "\n", encoding="utf-8"
        )
        output = temporary / "runtime.tar.gz"
        with tarfile.open(output, "w:gz") as archive:
            archive.add(root, arcname=RUNTIME_ROOT)
        if not static_archive_ready(output):
            raise RuntimeError("generated static verifier runtime is invalid")
        os.replace(output, target)
    print(f"[prepare] built static verifier Python: {target}")
