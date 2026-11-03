"""Bounded, verified file transfer over the DSec execution API.

Uploads keep base64 arguments within lifecycle argv limits, write a sibling
temporary file, verify it, and replace atomically. Downloads read committed
output in chunks and verify size and SHA-256 before replacement. Directory
archives are generated or fully validated here; devices, FIFOs, and path escape
are rejected. Host paths are never exposed to the Sandbox runtime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

UPLOAD_CHUNK_BYTES = 192 << 10
DOWNLOAD_CHUNK_BYTES = 384 << 10
ARGV_PART_BYTES = 3500
TRANSFER_OUTPUT_LIMIT_BYTES = 1 << 20

_CREATE_TEMP_SCRIPT = """\
import os,pathlib,sys
p=pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True,exist_ok=True)
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.close(fd)
"""

_APPEND_CHUNK_SCRIPT = """\
import base64,os,sys
p=sys.argv[1]
offset=int(sys.argv[2])
data=base64.b64decode(''.join(sys.argv[3:]),validate=True)
with open(p,'r+b') as f:
    f.seek(0,os.SEEK_END)
    assert f.tell()==offset,'upload offset mismatch'
    f.write(data)
"""

_FINALIZE_UPLOAD_SCRIPT = """\
import hashlib,os,sys
temporary,target,size,digest,mode=sys.argv[1:]
assert os.path.getsize(temporary)==int(size),'upload size mismatch'
h=hashlib.sha256()
with open(temporary,'rb') as f:
    for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
assert h.hexdigest()==digest,'upload checksum mismatch'
with open(temporary,'rb') as f: os.fsync(f.fileno())
os.chmod(temporary,int(mode,8))
os.replace(temporary,target)
"""

_FILE_METADATA_SCRIPT = """\
import hashlib,json,os,stat,sys
p=sys.argv[1]
s=os.stat(p,follow_symlinks=True)
assert stat.S_ISREG(s.st_mode),'source is not a regular file'
h=hashlib.sha256()
with open(p,'rb') as f:
    for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
print(json.dumps({'size':s.st_size,'sha256':h.hexdigest(),'mode':stat.S_IMODE(s.st_mode)},separators=(',',':')))
"""

_READ_CHUNK_SCRIPT = """\
import base64,sys
path,offset,length=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
with open(path,'rb') as f:
    f.seek(offset)
    data=f.read(length)
assert len(data)==length,'short file read'
sys.stdout.write(base64.b64encode(data).decode('ascii'))
"""

_UNLINK_SCRIPT = """\
import pathlib,sys
pathlib.Path(sys.argv[1]).unlink(missing_ok=True)
"""


def _remote_path(value: str) -> str:
    """Accept only normalized absolute POSIX paths as remote transfer paths."""
    if not value or "\0" in value or not value.startswith("/"):
        raise ValueError("DSec transfer target must be an absolute POSIX path")
    normalized = posixpath.normpath(value)
    if normalized != value.rstrip("/") and value != "/":
        raise ValueError("DSec transfer path must be normalized")
    return normalized


def _validated_tar_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Reject members that escape the root and skip devices and FIFOs."""
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


def _pack_directory(source: Path, archive_path: Path, maximum_bytes: int) -> None:
    """Pack a directory while bounding logical size, special files, and links."""
    logical_bytes = 0
    for path in source.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            logical_bytes += info.st_size
            if logical_bytes > maximum_bytes:
                raise ValueError("DSec directory transfer exceeds configured limit")
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError("DSec directory transfer contains a special file")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname=".", filter=_validated_tar_member)
    if archive_path.stat().st_size > maximum_bytes:
        raise ValueError("DSec directory archive exceeds configured limit")


def _extract_directory(archive_path: Path, target: Path) -> None:
    """Validate and extract into empty staging before merging into Harbor output."""
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".harbor-dsec-extract-", dir=target.parent
    ) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [
                member
                for item in archive.getmembers()
                if (member := _validated_tar_member(item)) is not None
            ]
            archive.extractall(staging, members=members)
        shutil.copytree(staging, target, dirs_exist_ok=True, symlinks=True)


class DsecTransferMixin:
    """Implement bounded file and directory transfer through small executions."""

    async def _run_transfer_argv(
        self,
        argv: list[str],
        *,
        timeout_sec: int = 180,
        output_limit_bytes: int = TRANSFER_OUTPUT_LIMIT_BYTES,
    ) -> bytes:
        record, stdout, _stderr = await self._execute_argv(
            argv,
            cwd="/",
            timeout_sec=timeout_sec,
            output_limit_bytes=output_limit_bytes,
        )
        if record.get("state") != "succeeded" or record.get("exit_code") != 0:
            raise RuntimeError("DSec transfer command failed")
        return stdout

    async def _cleanup_remote_file(self, path: str) -> None:
        try:
            await self._run_transfer_argv(
                ["python3", "-c", _UNLINK_SCRIPT, path], timeout_sec=30
            )
        except Exception:  # noqa: BLE001 - exact temporary cleanup is best effort
            self.logger.warning("failed to remove DSec transfer temporary file")

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"DSec upload source is not a regular file: {source}")
        size = source.stat().st_size
        if size > self._transfer_max_bytes:
            raise ValueError("DSec file upload exceeds configured limit")
        target = _remote_path(target_path)
        parent = str(PurePosixPath(target).parent)
        temporary = f"{parent}/.harbor-upload-{uuid.uuid4().hex}.part"
        await self._run_transfer_argv(
            ["python3", "-c", _CREATE_TEMP_SCRIPT, temporary], timeout_sec=60
        )
        digest = hashlib.sha256()
        offset = 0
        handle = await asyncio.to_thread(source.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, UPLOAD_CHUNK_BYTES):
                digest.update(chunk)
                encoded = base64.b64encode(chunk).decode("ascii")
                parts = [
                    encoded[index : index + ARGV_PART_BYTES]
                    for index in range(0, len(encoded), ARGV_PART_BYTES)
                ]
                await self._run_transfer_argv(
                    [
                        "python3",
                        "-c",
                        _APPEND_CHUNK_SCRIPT,
                        temporary,
                        str(offset),
                        *parts,
                    ]
                )
                offset += len(chunk)
            mode = f"{stat.S_IMODE(source.stat().st_mode):o}"
            await self._run_transfer_argv(
                [
                    "python3",
                    "-c",
                    _FINALIZE_UPLOAD_SCRIPT,
                    temporary,
                    target,
                    str(size),
                    digest.hexdigest(),
                    mode,
                ]
            )
        except BaseException:
            await self._cleanup_remote_file(temporary)
            raise
        finally:
            await asyncio.to_thread(handle.close)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"DSec upload source is not a directory: {source}")
        target = _remote_path(target_dir)
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-upload-") as temporary:
            archive = Path(temporary) / "directory.tar.gz"
            _pack_directory(source, archive, self._transfer_max_bytes)
            remote_archive = f"/tmp/.harbor-upload-{uuid.uuid4().hex}.tar.gz"
            try:
                await self.upload_file(archive, remote_archive)
                result = await self.exec(
                    f"mkdir -p {self._quote(target)} && "
                    f"tar xzf {self._quote(remote_archive)} -C {self._quote(target)}",
                    cwd="/",
                    timeout_sec=self._transfer_timeout_sec,
                    user="root",
                )
                if result.return_code != 0:
                    raise RuntimeError("failed to extract DSec directory upload")
            finally:
                await self._cleanup_remote_file(remote_archive)

    async def _remote_file_metadata(self, source_path: str) -> dict[str, Any]:
        output = await self._run_transfer_argv(
            ["python3", "-c", _FILE_METADATA_SCRIPT, source_path]
        )
        try:
            metadata = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("invalid DSec download metadata") from error
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("size"), int)
            or metadata["size"] < 0
            or metadata["size"] > self._transfer_max_bytes
            or not isinstance(metadata.get("sha256"), str)
            or len(metadata["sha256"]) != 64
            or not isinstance(metadata.get("mode"), int)
        ):
            raise ValueError("DSec download exceeds limits or has invalid metadata")
        return metadata

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        source = _remote_path(source_path)
        metadata = await self._remote_file_metadata(source)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".harbor-download-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                for offset in range(0, metadata["size"], DOWNLOAD_CHUNK_BYTES):
                    length = min(DOWNLOAD_CHUNK_BYTES, metadata["size"] - offset)
                    encoded = await self._run_transfer_argv(
                        [
                            "python3",
                            "-c",
                            _READ_CHUNK_SCRIPT,
                            source,
                            str(offset),
                            str(length),
                        ]
                    )
                    try:
                        chunk = base64.b64decode(encoded, validate=True)
                    except ValueError as error:
                        raise RuntimeError("invalid DSec download chunk") from error
                    if len(chunk) != length:
                        raise RuntimeError("DSec download returned a short chunk")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != metadata["sha256"]:
                raise RuntimeError("DSec download checksum mismatch")
            temporary.chmod(metadata["mode"])
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        source = _remote_path(source_dir)
        remote_archive = f"/tmp/.harbor-download-{uuid.uuid4().hex}.tar.gz"
        try:
            result = await self.exec(
                f"tar czf {self._quote(remote_archive)} -C {self._quote(source)} .",
                cwd="/",
                timeout_sec=self._transfer_timeout_sec,
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError("failed to create DSec directory download archive")
            with tempfile.TemporaryDirectory(prefix="harbor-dsec-download-") as temporary:
                archive = Path(temporary) / "directory.tar.gz"
                await self.download_file(remote_archive, archive)
                _extract_directory(archive, Path(target_dir))
        finally:
            await self._cleanup_remote_file(remote_archive)

    async def _materialize_read_only_mounts(self) -> None:
        for mount in getattr(self, "_mounts", []):
            # Harbor always supplies its writable log/artifact binds. Remote
            # environments expose those paths in-container and download them
            # through the environment API instead of mounting the host paths.
            if not mount.get("read_only"):
                continue
            source = Path(str(mount["source"]))
            target = _remote_path(str(mount["target"]))
            if source.is_dir():
                await self.upload_dir(source, target)
            elif source.is_file():
                await self.upload_file(source, target)
            else:
                raise ValueError(f"DSec mount source does not exist: {source}")
            result = await self.exec(
                f"chmod -R a-w {self._quote(target)}",
                cwd="/",
                timeout_sec=120,
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError("failed to make DSec materialized input read-only")

    @staticmethod
    def _quote(value: str) -> str:
        """Quote a path so it remains one shell argument."""
        import shlex

        return shlex.quote(value)
