"""Content-addressed S3 staging for YiCloud OpenSandbox artifacts.

Hosts retain deterministic local payloads and anonymous-read object URLs.
Development hosts may optionally provide S3 write credentials;
read-only users can reuse objects that have already been published.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

BUFFER_SIZE = 4 * 1024 * 1024
SAMPLE_SIZE = 4 * 1024 * 1024
ANONYMOUS_PROBE_TIMEOUT_SEC = 15


def _direct_urlopen(request: Request, timeout: int):
    # The read origin is an internal S3 endpoint. Bypass ambient development-host
    # proxies so they cannot turn a
    # valid object response into a gateway error.
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(handle: BinaryIO) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise


def _safe_prefix(value: str) -> str:
    raw = value.strip().strip("/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(
            "YICLOUD_SANDBOX_S3_PREFIX must be a safe relative prefix"
        )
    return raw


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {value!r}")
    return path


def _positive_int(value: str | int, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


@dataclass(frozen=True)
class S3UploadArtifact:
    kind: str
    logical_digest: str
    payload_digest: str
    payload_size: int
    compression: str
    local_payload_path: str
    object_key: str
    object_uri: str
    download_url: str = ""


class S3WriteUnavailableError(RuntimeError):
    """The immutable object is absent and no safe writer is configured."""


class _AnonymousS3ProbeForbiddenError(RuntimeError):
    """An anonymous probe could not distinguish a missing object."""


class S3UploadStore:
    """Build deterministic payloads and publish anonymous-read object URLs.

    Anonymous reads are the primary capability. ``config_path`` is optional
    and is consulted only when a content-addressed object is not already
    available through ``read_origin``.
    """

    def __init__(
        self,
        *,
        config_path: Path | str | None = None,
        bucket: str,
        read_origin: str,
        prefix: str = "agent-fleet-upload/v1",
        cache_root: Path | str = "/data/harbor-runs/opensandbox-s3-cache",
        lock_root: Path | str = "/data/harbor-runs/opensandbox-s3-locks",
        directory_compression: str = "auto",
        s3cmd: str = "s3cmd",
    ) -> None:
        raw_config_path = str(config_path or "").strip()
        self.config_path = (
            _absolute_path(raw_config_path, "YICLOUD_SANDBOX_S3_CONFIG")
            if raw_config_path
            else None
        )
        self.bucket = bucket.strip()
        if not self.bucket or "/" in self.bucket:
            raise ValueError("YICLOUD_SANDBOX_S3_BUCKET is invalid")
        self.read_origin = self._validate_read_origin(read_origin)
        self.prefix = _safe_prefix(prefix)
        self.cache_root = _absolute_path(
            str(cache_root), "YICLOUD_SANDBOX_S3_CACHE_ROOT"
        )
        self.lock_root = _absolute_path(
            str(lock_root), "YICLOUD_SANDBOX_S3_LOCK_ROOT"
        )
        compression = directory_compression.strip().lower()
        if compression not in {"auto", "none", "gzip"}:
            raise ValueError(
                "YICLOUD_SANDBOX_S3_DIRECTORY_COMPRESSION must be "
                "auto, none, or gzip"
            )
        self.directory_compression = compression
        self.s3cmd = s3cmd.strip()
        if not self.s3cmd:
            raise ValueError("YICLOUD_SANDBOX_S3CMD must not be empty")

    @classmethod
    def from_environment(cls) -> S3UploadStore:
        return cls(
            config_path=os.environ.get("YICLOUD_SANDBOX_S3_CONFIG", ""),
            bucket=os.environ.get("YICLOUD_SANDBOX_S3_BUCKET", ""),
            read_origin=os.environ.get(
                "YICLOUD_SANDBOX_S3_READ_ORIGIN", ""
            ),
            prefix=os.environ.get(
                "YICLOUD_SANDBOX_S3_PREFIX",
                "agent-fleet-upload/v1",
            ),
            cache_root=os.environ.get(
                "YICLOUD_SANDBOX_S3_CACHE_ROOT",
                "/data/harbor-runs/opensandbox-s3-cache",
            ),
            lock_root=os.environ.get(
                "YICLOUD_SANDBOX_S3_LOCK_ROOT",
                "/data/harbor-runs/opensandbox-s3-locks",
            ),
            directory_compression=os.environ.get(
                "YICLOUD_SANDBOX_S3_DIRECTORY_COMPRESSION",
                "auto",
            ),
            s3cmd=os.environ.get("YICLOUD_SANDBOX_S3CMD", "s3cmd"),
        )

    @staticmethod
    def _validate_read_origin(value: str) -> str:
        origin = value.strip().rstrip("/")
        parsed = urlsplit(origin)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(
                "YICLOUD_SANDBOX_S3_READ_ORIGIN has an invalid port"
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(character.isspace() for character in origin)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "YICLOUD_SANDBOX_S3_READ_ORIGIN must be an HTTP(S) URL "
                "without credentials, query, or fragment"
            )
        return origin

    def _executable(self) -> str:
        if "/" in self.s3cmd:
            if not Path(self.s3cmd).is_file():
                raise FileNotFoundError(self.s3cmd)
            return self.s3cmd
        resolved = shutil.which(self.s3cmd)
        if resolved is None:
            raise FileNotFoundError(
                f"S3 upload backend requires {self.s3cmd!r} on PATH"
            )
        return resolved

    def _writer_config_path(self) -> Path:
        config_path = self.config_path
        if config_path is None:
            raise S3WriteUnavailableError(
                "S3 object is not anonymously readable and no write "
                "configuration is available"
            )
        if config_path.is_symlink() or not config_path.is_file():
            raise FileNotFoundError(
                f"S3 write configuration is not a regular file: {config_path}"
            )
        if stat.S_IMODE(config_path.stat().st_mode) & 0o077:
            raise PermissionError(
                "S3 write configuration must not be group/world-readable: "
                f"{config_path}"
            )
        return config_path

    def _run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # Validate at the point of every credentialed operation. Preflight and
        # runtime staging execute in separate processes, so preflight alone is
        # not a security boundary.
        config_path = self._writer_config_path()
        completed = subprocess.run(
            [
                self._executable(),
                "-c",
                str(config_path),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()[:1000]
            raise RuntimeError(
                f"s3cmd {' '.join(args[:1])} failed "
                f"with exit code {completed.returncode}: {message}"
            )
        return completed

    def preflight(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        if self.config_path is not None:
            self._run("--version")

    @contextlib.contextmanager
    def _digest_lock(
        self, kind: str, digest: str, compression: str
    ) -> Iterator[None]:
        lock_path = (
            self.lock_root
            / "locks"
            / kind
            / f"{digest}-{compression}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _artifact_dir(
        self, kind: str, logical_digest: str, compression: str
    ) -> Path:
        return (
            self.cache_root
            / self.prefix
            / "objects"
            / kind
            / "sha256"
            / logical_digest[:2]
            / logical_digest
            / compression
        )

    def _object_key(
        self,
        kind: str,
        logical_digest: str,
        payload_digest: str,
        payload_name: str,
    ) -> str:
        return (
            f"{self.prefix}/objects/{kind}/sha256/{logical_digest[:2]}/"
            f"{logical_digest}/{payload_digest}/{payload_name}"
        )

    def _load_artifact(self, artifact_dir: Path) -> S3UploadArtifact | None:
        manifest_path = artifact_dir / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload_name = str(data["payload_name"])
            if Path(payload_name).name != payload_name:
                return None
            payload = artifact_dir / payload_name
            payload_size = int(data["payload_size"])
            payload_digest = str(data["payload_digest"])
            if (
                not payload.is_file()
                or payload.stat().st_size != payload_size
                or _sha256_file(payload) != payload_digest
            ):
                return None
            object_key = str(data["object_key"])
            return S3UploadArtifact(
                kind=str(data["kind"]),
                logical_digest=str(data["logical_digest"]),
                payload_digest=payload_digest,
                payload_size=payload_size,
                compression=str(data["compression"]),
                local_payload_path=str(payload),
                object_key=object_key,
                object_uri=f"s3://{self.bucket}/{object_key}",
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _commit_artifact(
        self,
        *,
        temporary_dir: Path,
        final_dir: Path,
        kind: str,
        logical_digest: str,
        payload: Path,
        compression: str,
    ) -> S3UploadArtifact:
        payload_digest = _sha256_file(payload)
        payload_size = payload.stat().st_size
        object_key = self._object_key(
            kind,
            logical_digest,
            payload_digest,
            payload.name,
        )
        manifest = {
            "schema": 1,
            "kind": kind,
            "logical_digest": logical_digest,
            "payload_digest": payload_digest,
            "payload_size": payload_size,
            "compression": compression,
            "payload_name": payload.name,
            "object_key": object_key,
        }
        manifest_path = temporary_dir / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_dir, final_dir)
        artifact = self._load_artifact(final_dir)
        if artifact is None:
            raise RuntimeError(f"failed to reopen S3 upload cache: {final_dir}")
        return artifact

    def _anonymous_remote_size(
        self, artifact: S3UploadArtifact
    ) -> tuple[bool, int | None]:
        request = Request(
            self._download_url(artifact),
            method="HEAD",
            headers={"User-Agent": "agent-fleet-opensandbox/1"},
        )
        try:
            with _direct_urlopen(
                request, timeout=ANONYMOUS_PROBE_TIMEOUT_SEC
            ) as response:
                raw_size = response.headers.get("Content-Length")
        except HTTPError as exc:
            if exc.code == 404:
                return False, None
            if exc.code == 403:
                raise _AnonymousS3ProbeForbiddenError(
                    "anonymous S3 object probe was forbidden: "
                    f"key={artifact.object_key!r}"
                ) from exc
            raise RuntimeError(
                "anonymous S3 object probe failed: "
                f"key={artifact.object_key!r} status={exc.code}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                "anonymous S3 object probe failed: "
                f"key={artifact.object_key!r} error={exc}"
            ) from exc
        if raw_size is None:
            return True, None
        try:
            return True, int(raw_size)
        except ValueError as exc:
            raise RuntimeError(
                "anonymous S3 object returned an invalid Content-Length: "
                f"key={artifact.object_key!r} value={raw_size!r}"
            ) from exc

    @staticmethod
    def _validate_remote_size(
        artifact: S3UploadArtifact, remote_size: int | None
    ) -> None:
        if remote_size is not None and remote_size != artifact.payload_size:
            raise RuntimeError(
                "content-addressed S3 object has an unexpected size: "
                f"uri={artifact.object_uri!r} "
                f"expected={artifact.payload_size} actual={remote_size}"
            )

    def _authenticated_object_exists(self, artifact: S3UploadArtifact) -> bool:
        """Resolve an anonymous 403 without granting public bucket listing."""
        completed = self._run("ls", artifact.object_uri)
        for line in completed.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) == 4 and fields[3] == artifact.object_uri:
                return True
        return False

    def _ensure_remote(self, artifact: S3UploadArtifact) -> None:
        try:
            exists, remote_size = self._anonymous_remote_size(artifact)
        except _AnonymousS3ProbeForbiddenError as exc:
            if self._authenticated_object_exists(artifact):
                raise RuntimeError(
                    "content-addressed S3 object exists but is not "
                    "anonymously readable: "
                    f"key={artifact.object_key!r}"
                ) from exc
            exists, remote_size = False, None
        if exists:
            self._validate_remote_size(artifact, remote_size)
            return

        # A public 404 or an authenticated exact-key miss enables the optional
        # writer. Existing public objects never require or expose credentials.
        self._run(
            "--no-progress",
            "put",
            artifact.local_payload_path,
            artifact.object_uri,
        )
        try:
            exists, remote_size = self._anonymous_remote_size(artifact)
        except _AnonymousS3ProbeForbiddenError as exc:
            raise RuntimeError(
                "S3 upload completed but the object is not anonymously "
                f"readable: key={artifact.object_key!r}"
            ) from exc
        if not exists:
            raise RuntimeError(
                "S3 upload completed but the object is not anonymously readable: "
                f"key={artifact.object_key!r}"
            )
        self._validate_remote_size(artifact, remote_size)

    def _download_url(self, artifact: S3UploadArtifact) -> str:
        return f"{self.read_origin}/{quote(artifact.object_key, safe='/')}"

    def _publish(self, artifact: S3UploadArtifact) -> S3UploadArtifact:
        self._ensure_remote(artifact)
        return replace(artifact, download_url=self._download_url(artifact))

    def stage_file(self, source_path: Path | str) -> S3UploadArtifact:
        source = Path(source_path)
        if not source.is_file():
            raise ValueError(f"S3 upload source is not a file: {source}")
        logical_digest = _sha256_file(source)
        compression = "none"
        artifact_dir = self._artifact_dir(
            "file", logical_digest, compression
        )
        with self._digest_lock("file", logical_digest, compression):
            cached = self._load_artifact(artifact_dir)
            if cached is None:
                artifact_dir.parent.mkdir(parents=True, exist_ok=True)
                temporary_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".{artifact_dir.name}.",
                        dir=artifact_dir.parent,
                    )
                )
                try:
                    payload = temporary_dir / "payload"
                    with source.open("rb") as incoming, payload.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=BUFFER_SIZE)
                        _fsync_file(outgoing)
                    if _sha256_file(payload) != logical_digest:
                        raise RuntimeError(
                            f"S3 upload file changed while snapshotting: {source}"
                        )
                    cached = self._commit_artifact(
                        temporary_dir=temporary_dir,
                        final_dir=artifact_dir,
                        kind="file",
                        logical_digest=logical_digest,
                        payload=payload,
                        compression=compression,
                    )
                finally:
                    shutil.rmtree(temporary_dir, ignore_errors=True)
            return self._publish(cached)

    def _tree_digest(self, source: Path) -> tuple[str, int, int]:
        digest = hashlib.sha256()
        logical_size = 0
        file_count = 0
        root_info = source.lstat()
        digest.update(
            json.dumps(
                [".", "directory", stat.S_IMODE(root_info.st_mode), 0, ""],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(source).as_posix()
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                kind = "directory"
                content_digest = ""
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                content_digest = _sha256_file(path)
                logical_size += info.st_size
                file_count += 1
            elif stat.S_ISLNK(info.st_mode):
                kind = "symlink"
                content_digest = os.readlink(path)
            else:
                raise ValueError(
                    f"unsupported S3 upload directory entry: {path}"
                )
            digest.update(
                json.dumps(
                    [relative, kind, mode, info.st_size, content_digest],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
        return digest.hexdigest(), logical_size, file_count

    def _compression_for(self, source: Path) -> str:
        if self.directory_compression != "auto":
            return self.directory_compression
        sample = bytearray()
        files = sorted(
            (
                path
                for path in source.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (-path.stat().st_size, path.as_posix()),
        )
        per_file = max(1, SAMPLE_SIZE // min(len(files), 32)) if files else 0
        for path in files[:32]:
            remaining = SAMPLE_SIZE - len(sample)
            if remaining <= 0:
                break
            with path.open("rb") as handle:
                sample.extend(handle.read(min(per_file, remaining)))
        if not sample:
            return "none"
        compressed = zlib.compress(bytes(sample), level=1)
        return "gzip" if len(compressed) <= int(len(sample) * 0.9) else "none"

    @staticmethod
    def _normalized_tarinfo(tar: tarfile.TarFile, path: Path, arcname: str):
        info = tar.gettarinfo(str(path), arcname=arcname)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        return info

    def _write_directory_archive(
        self, source: Path, payload: Path, compression: str
    ) -> None:
        paths = sorted(source.rglob("*"), key=lambda item: item.as_posix())

        def populate(tar: tarfile.TarFile) -> None:
            tar.addfile(self._normalized_tarinfo(tar, source, "."))
            for path in paths:
                relative = path.relative_to(source).as_posix()
                info = self._normalized_tarinfo(tar, path, relative)
                if info.isreg():
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)
                else:
                    tar.addfile(info)

        if compression == "gzip":
            with payload.open("xb") as raw:
                with gzip.GzipFile(
                    fileobj=raw,
                    mode="wb",
                    compresslevel=1,
                    mtime=0,
                ) as compressed, tarfile.open(fileobj=compressed, mode="w|") as tar:
                    populate(tar)
                _fsync_file(raw)
        else:
            with payload.open("xb") as raw:
                with tarfile.open(fileobj=raw, mode="w|") as tar:
                    populate(tar)
                _fsync_file(raw)

    def stage_directory(self, source_path: Path | str) -> S3UploadArtifact:
        source = Path(source_path)
        if not source.is_dir():
            raise ValueError(f"S3 upload source is not a directory: {source}")
        logical_digest, logical_size, file_count = self._tree_digest(source)
        compression = self._compression_for(source)
        artifact_dir = self._artifact_dir(
            "directory", logical_digest, compression
        )
        with self._digest_lock("directory", logical_digest, compression):
            cached = self._load_artifact(artifact_dir)
            if cached is None:
                artifact_dir.parent.mkdir(parents=True, exist_ok=True)
                temporary_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".{artifact_dir.name}.",
                        dir=artifact_dir.parent,
                    )
                )
                try:
                    suffix = ".tar.gz" if compression == "gzip" else ".tar"
                    payload = temporary_dir / f"payload{suffix}"
                    self._write_directory_archive(source, payload, compression)
                    verified = self._tree_digest(source)
                    if verified != (logical_digest, logical_size, file_count):
                        raise RuntimeError(
                            "S3 upload directory changed while snapshotting: "
                            f"{source}"
                        )
                    cached = self._commit_artifact(
                        temporary_dir=temporary_dir,
                        final_dir=artifact_dir,
                        kind="directory",
                        logical_digest=logical_digest,
                        payload=payload,
                        compression=compression,
                    )
                finally:
                    shutil.rmtree(temporary_dir, ignore_errors=True)
            return self._publish(cached)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "prewarm"])
    parser.add_argument("sources", nargs="*")
    args = parser.parse_args()
    store = S3UploadStore.from_environment()
    store.preflight()
    if args.command == "prewarm":
        for value in args.sources:
            source = Path(value)
            artifact = (
                store.stage_directory(source)
                if source.is_dir()
                else store.stage_file(source)
            )
            print(
                "[s3-upload] prewarmed "
                f"kind={artifact.kind} "
                f"digest={artifact.logical_digest} "
                f"size_bytes={artifact.payload_size}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
