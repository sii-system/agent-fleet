"""Compose and validate the SWE-rebench-V2 verifier runtime bundle."""

from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from .. import python_runtime
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from verifier_runtime import python_runtime

BUNDLE_ID = "agent-fleet-swe-rebench-v2-verifier-bundle"
SELF_CHECK = "bin/harbor-verifier-bundle-check"
SELF_CHECK_SOURCE = Path(__file__).with_name("self_check.sh")
SELF_CHECK_PY_SOURCE = Path(__file__).with_name("self_check.py")


def _self_check_content() -> bytes:
    """Read the shell dispatcher that is embedded as the bundle entrypoint."""
    return SELF_CHECK_SOURCE.read_bytes()


def _members(path: Path) -> dict[str, tarfile.TarInfo] | None:
    """Return archive members by normalized name, or None for an invalid tarball."""
    if not path.is_file():
        return None
    try:
        with tarfile.open(path) as archive:
            return {member.name.rstrip("/"): member for member in archive}
    except (OSError, tarfile.TarError):
        return None


def archive_ready(path: Path) -> bool:
    """Return whether path is a complete, content-matching verifier bundle."""
    if not python_runtime.static_archive_ready(path, BUNDLE_ID):
        return False
    members = _members(path)
    if members is None:
        return False
    bin_root = f"{BUNDLE_ID}/bin"
    check = members.get(f"{BUNDLE_ID}/{SELF_CHECK}")
    python312 = members.get(f"{bin_root}/python3.12")
    python3 = members.get(f"{bin_root}/python3")
    python = members.get(f"{bin_root}/python")
    try:
        with tarfile.open(path) as archive:
            script = (
                archive.extractfile(f"{BUNDLE_ID}/{SELF_CHECK}")
                if check and check.isfile()
                else None
            )
            if script is None or script.read() != _self_check_content():
                return False
            python_script = archive.extractfile(
                f"{BUNDLE_ID}/bin/self_check.py"
            )
            if python_script is None or python_script.read() != SELF_CHECK_PY_SOURCE.read_bytes():
                return False
    except (OSError, KeyError, tarfile.TarError):
        return False
    return bool(
        check
        and check.isfile()
        and check.mode & 0o111
        and python312
        and python312.isfile()
        and python312.mode & 0o111
        and python3
        and python3.issym()
        and python3.linkname == "python3.12"
        and python
        and python.issym()
        and python.linkname == "python3.12"
    )


def _extract_safely(archive_path: Path, destination: Path) -> None:
    """Extract a runtime archive while rejecting paths and links leaving root.

    The standalone distribution contains a few absolute documentation or
    terminfo links that are irrelevant to Python execution.  Those links are
    skipped; relative links are retained only when their normalized target
    remains inside the extraction root.
    """
    with tarfile.open(archive_path) as archive:
        for member in archive:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"unsafe archive member: {member.name}")
            if member.issym():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute():
                    continue
                resolved = PurePosixPath(
                    posixpath.normpath(
                        str(member_path.parent / link_path)
                    )
                )
                if ".." in resolved.parts:
                    continue
            elif member.islnk():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute() or ".." in link_path.parts:
                    raise RuntimeError(f"unsafe archive link: {member.name}")
            if sys.version_info >= (3, 12):
                archive.extract(member, destination, filter="data")
            else:
                archive.extract(member, destination)


def build(python_runtime_archive: Path, output: Path) -> None:
    """Compose a verifier bundle from a validated static-runtime archive."""
    if archive_ready(output):
        print(f"[prepare] skip verifier runtime bundle (cached): {output}")
        return
    if not python_runtime.static_archive_ready(python_runtime_archive):
        raise RuntimeError(
            f"invalid Python runtime archive: {python_runtime_archive}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        _extract_safely(python_runtime_archive, temporary)
        source_root = temporary / python_runtime.RUNTIME_ROOT
        bundle_root = temporary / BUNDLE_ID
        source_root.rename(bundle_root)

        runtime_bin = bundle_root / "bin"
        for name in ("python", "python3", "harbor-verifier-bundle-check"):
            (runtime_bin / name).unlink(missing_ok=True)
        (runtime_bin / "python3").symlink_to("python3.12")
        (runtime_bin / "python").symlink_to("python3.12")
        self_check = runtime_bin / "harbor-verifier-bundle-check"
        self_check.write_bytes(_self_check_content())
        shutil.copy2(SELF_CHECK_PY_SOURCE, runtime_bin / "self_check.py")
        self_check.chmod(
            self_check.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

        file_descriptor, temporary_tar_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(file_descriptor)
        temporary_tar = Path(temporary_tar_name)
        try:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(bundle_root, arcname=BUNDLE_ID)
            if not archive_ready(temporary_tar):
                raise RuntimeError("generated verifier runtime bundle is invalid")
            os.replace(temporary_tar, output)
        finally:
            temporary_tar.unlink(missing_ok=True)
    print(f"[prepare] built verifier runtime bundle: {output}")


def prepare(cache_dir: Path, output: Path) -> None:
    """Build or reuse the static runtime in cache_dir, then compose output."""
    python_runtime_archive = cache_dir / "python3.12-static-runtime.tar.gz"
    python_runtime.build_static(python_runtime_archive)
    build(python_runtime_archive, output)


def main() -> int:
    """Run the directory-package CLI used by Harbor dependency preparation."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--cache-dir", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "check":
        return 0 if archive_ready(args.archive) else 1
    prepare(args.cache_dir, args.output)
    return 0


__all__ = ["BUNDLE_ID", "SELF_CHECK", "archive_ready", "build", "main", "prepare"]
