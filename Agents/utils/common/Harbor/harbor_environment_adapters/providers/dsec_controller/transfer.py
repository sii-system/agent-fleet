"""Transfer files through native Controller archives using only host-side tools.

Moby owns filesystem access and extraction. Uploads follow Docker archive copy
semantics: a failed upload may have partial side effects and is never retried.
Downloads are validated and staged on the host before committing local output.
No transfer, preparation, or cleanup step executes a guest program.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from async_utils import await_owned

from .archive_client import MAX_ARCHIVE_BYTES


def _remote_path(value: str) -> str:
    """Accept normalized absolute container paths, never host paths or traversal."""
    if not value or "\0" in value or not value.startswith("/"):
        raise ValueError("DSec transfer target must be an absolute POSIX path")
    normalized = posixpath.normpath(value)
    if normalized != value.rstrip("/") and value != "/":
        raise ValueError("DSec transfer path must be normalized")
    return normalized


def _validated_tar_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Reject traversal and escaping links; skip devices and FIFOs."""
    if member.isdev() or member.isfifo():
        return None
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("transfer archive contains an unsafe member path")
    if member.issym() or member.islnk():
        link = PurePosixPath(member.linkname)
        if link.is_absolute():
            raise ValueError("transfer archive contains an absolute link")
        base = name.parent if member.issym() else PurePosixPath(".")
        resolved = posixpath.normpath(str(base / link))
        if resolved == ".." or resolved.startswith("../"):
            raise ValueError("transfer archive link escapes its root")
    return member


def _archive_filter(read_only: bool):
    """Set container ownership and read-only modes before upload, on the host."""
    def prepare(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        member = _validated_tar_member(member)
        if member is not None:
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            if read_only:
                member.mode &= ~0o222
        return member
    return prepare


def _check_archive_size(archive: Path, maximum_bytes: int) -> None:
    if archive.stat().st_size > min(maximum_bytes, MAX_ARCHIVE_BYTES):
        raise ValueError("DSec archive transfer exceeds configured limit")


def _extract_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    """Keep data-filter path/link protection without adding owner write access."""
    safe = tarfile.data_filter(member, destination)
    if member.isfile() or member.isdir() or member.islnk():
        safe = safe.replace(mode=member.mode & 0o777)
    return safe


def _pack_directory(
    source: Path, archive_path: Path, maximum_bytes: int,
    *, arcname: str = ".", read_only: bool = False,
) -> None:
    """Bound logical bytes and reject links escaping the input before packing."""
    logical_bytes = 0
    for path in source.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            logical_bytes += info.st_size
            if logical_bytes > maximum_bytes:
                raise ValueError("DSec directory transfer exceeds configured limit")
        elif stat.S_ISLNK(info.st_mode):
            if not path.resolve().is_relative_to(source.resolve()):
                raise ValueError("transfer archive link escapes its root")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("DSec directory transfer contains a special file")
    with tarfile.open(archive_path, "w") as archive:
        archive.add(source, arcname=arcname, filter=_archive_filter(read_only))
    _check_archive_size(archive_path, maximum_bytes)


def _extract_directory(
    archive_path: Path, target: Path, maximum_bytes: int = MAX_ARCHIVE_BYTES,
    *, source_name: str = ".", container_root: bool = False,
) -> None:
    """Validate into host staging before merging a Docker directory archive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".harbor-dsec-extract-", dir=target.parent) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive_path, "r:*") as archive:
            members = []
            for item in archive.getmembers():
                if container_root:
                    # Moby rebases a root export to / and /path. Rebase only
                    # this explicit request into host staging; keep traversal
                    # and absolute symlink checks. Update the owned TarInfo so
                    # tarfile's hardlink lookup uses the same relative names.
                    item.name = item.name.removeprefix("/") or "."
                    if item.islnk():
                        item.linkname = item.linkname.removeprefix("/") or "."
                if (member := _validated_tar_member(item)) is not None:
                    members.append(member)
            if sum(member.size for member in members) > maximum_bytes:
                raise ValueError("DSec directory download exceeds configured limit")
            if source_name != "." and any(
                not PurePosixPath(member.name).is_relative_to(source_name)
                for member in members
            ):
                raise ValueError("DSec directory archive has an unexpected root")
            archive.extractall(staging, members=members, filter=_extract_filter)
        source = staging / source_name
        if not source.is_dir() or source.is_symlink():
            raise ValueError("DSec directory download is not a directory")
        # The exported subtree loses its staging prefix when copied. A link
        # contained in staging may therefore point outside the final output.
        # Resolve after all entries exist so link chains are checked as well.
        export_root = source.resolve()
        output_root = target.resolve()
        for member in members:
            # Existing collection output can contain links from an earlier
            # download; those must not redirect this merge into host siblings.
            destination = target / (staging / member.name).relative_to(source)
            if not destination.resolve().is_relative_to(output_root):
                raise ValueError("DSec directory output link escapes its collection directory")
            if member.issym():
                try:
                    resolved = (staging / member.name).resolve()
                    exported_link = (destination.parent / os.readlink(staging / member.name)).resolve()
                except (OSError, RuntimeError) as error:
                    raise ValueError("DSec directory archive contains an invalid link") from error
                if not resolved.is_relative_to(export_root) or not exported_link.is_relative_to(output_root):
                    raise ValueError("DSec directory archive link escapes its exported subtree")
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)


class DsecTransferMixin:
    """Prepare archives on the host and delegate filesystem access to Moby."""

    async def upload_file(
        self, source_path: Path | str, target_path: str, *, read_only: bool = False
    ) -> None:
        source = Path(source_path)
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"DSec upload source is not a regular file: {source}")
        if source.stat().st_size > self._transfer_max_bytes:
            raise ValueError("DSec file upload exceeds configured limit")
        target = _remote_path(target_path)
        if target == "/":
            raise ValueError("DSec file upload requires a file target")
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-upload-") as temporary:
            archive_path = Path(temporary) / "file.tar"
            with tarfile.open(archive_path, "w") as archive:
                archive.add(source, arcname=target.lstrip("/"), filter=_archive_filter(read_only))
            _check_archive_size(archive_path, self._transfer_max_bytes)
            await self._client.upload_archive(self._sandbox_id, "/", archive_path)

    async def upload_dir(
        self, source_dir: Path | str, target_dir: str, *, read_only: bool = False
    ) -> None:
        source = Path(source_dir)
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"DSec upload source is not a directory: {source}")
        # A single dot means the immutable OCI working directory, resolved by Node.
        target = "." if target_dir == "." else _remote_path(target_dir)
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-upload-") as temporary:
            archive_path = Path(temporary) / "directory.tar"
            await await_owned(asyncio.to_thread(
                _pack_directory, source, archive_path, self._transfer_max_bytes,
                arcname=target.lstrip("/") or ".", read_only=read_only,
            ))
            await self._client.upload_archive(
                self._sandbox_id, "." if target == "." else "/", archive_path
            )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        source = _remote_path(source_path)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".harbor-dsec-download-", dir=target.parent) as temporary:
            archive_path = Path(temporary) / "file.tar"
            await self._client.download_archive(
                self._sandbox_id, source, archive_path, self._transfer_max_bytes
            )
            with tarfile.open(archive_path, "r:") as archive:
                members = archive.getmembers()
                if (
                    len(members) != 1 or not members[0].isfile()
                    or members[0].name != PurePosixPath(source).name
                    or members[0].size > self._transfer_max_bytes
                ):
                    raise ValueError("DSec file archive is not one bounded regular file")
                member = members[0]
                staged = Path(temporary) / "payload"
                with archive.extractfile(member) as content, staged.open("xb") as output:
                    shutil.copyfileobj(content, output)
                    output.flush()
                    os.fsync(output.fileno())
                # Container ownership does not carry over to the host; never
                # restore setuid/setgid bits on a host-owned downloaded file.
                staged.chmod(member.mode & 0o777)
                staged.replace(target)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        source = _remote_path(source_dir)
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-download-") as temporary:
            archive_path = Path(temporary) / "directory.tar"
            await self._client.download_archive(
                self._sandbox_id, source, archive_path, self._transfer_max_bytes
            )
            await await_owned(asyncio.to_thread(
                _extract_directory, archive_path, Path(target_dir), self._transfer_max_bytes,
                source_name=PurePosixPath(source).name or ".",
                container_root=source == "/",
            ))

    async def _prepare_harbor_directories(self) -> None:
        """Create log/output directories with archive metadata, without guest mkdir."""
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-directories-") as temporary:
            archive_path = Path(temporary) / "directories.tar"
            with tarfile.open(archive_path, "w") as archive:
                for name in ("logs/agent", "logs/verifier", "logs/artifacts", "artifacts"):
                    member = tarfile.TarInfo(name)
                    member.type, member.mode = tarfile.DIRTYPE, 0o777
                    archive.addfile(member)
            await self._client.upload_archive(self._sandbox_id, "/", archive_path)

    async def _upload_environment_dir_after_start(self) -> None:
        """Resolve an unspecified workdir through Node metadata, never guest pwd."""
        from harbor.environments.definition import should_upload_environment_dir

        if should_upload_environment_dir(
            self.environment_dir, docker_image=self.task_env_config.docker_image
        ):
            await self.upload_dir(
                self.environment_dir, self.task_env_config.workdir or "."
            )

    async def _materialize_read_only_mounts(self) -> None:
        """Set read-only input modes in host tar headers, without guest chmod."""
        for mount in getattr(self, "_mounts", []):
            if not mount.get("read_only"):
                continue
            source = Path(str(mount["source"]))
            target = _remote_path(str(mount["target"]))
            if source.is_dir():
                await self.upload_dir(source, target, read_only=True)
            elif source.is_file():
                await self.upload_file(source, target, read_only=True)
            else:
                raise ValueError(f"DSec mount source does not exist: {source}")
