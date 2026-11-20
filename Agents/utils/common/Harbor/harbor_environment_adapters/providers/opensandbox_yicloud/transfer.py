"""YiCloud artifact preparation around S3 guest pull and native file transfers.

Immutable S3-staged artifacts prefer direct in-guest pull with SHA-256
verification and automatic fallback to host-initiated native HTTP upload when a
minimal container lacks download tools.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import shlex
import stat
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from artifact_collection import artifact_download_slot
from async_utils import await_owned

from .native_files import YiCloudNativeFilesMixin
from .packing import pack_directory, pack_directory_async
from .s3_transfer import S3UploadStore

_S3_GUEST_PULL_SCRIPT_PATH = (
    Path(__file__).resolve().parent / "scripts" / "s3_guest_pull.sh"
)
_ACTIVE_S3_STAGE_SOURCE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_ACTIVE_S3_STAGE_SOURCE", default=None
)


@functools.lru_cache(maxsize=1)
def _load_s3_guest_pull_script() -> str:
    return _S3_GUEST_PULL_SCRIPT_PATH.read_text(encoding="utf-8")


class YiCloudTransferMixin(YiCloudNativeFilesMixin):
    """Pack/extract task artifacts around host-owned native file requests."""

    def _fast_upload_url(self) -> str:
        if not self._sandbox_id:
            raise RuntimeError("YiCloud Sandbox is not running")
        origin = os.environ.get("YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", "").strip()
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN must be an HTTP(S) origin"
                )
            return (
                f"{origin.rstrip('/')}/sandboxes/{self._sandbox_id}"
                "/proxy/44772/files/upload"
            )

        command_url = getattr(self, "_command_url", "")
        if not command_url:
            return ""
        parsed = urlsplit(command_url)
        command_suffix = "/command"
        if not parsed.path.endswith(command_suffix):
            raise RuntimeError(
                "YiCloud execd command endpoint has an unexpected path: "
                f"{parsed.path!r}"
            )
        path = parsed.path[: -len(command_suffix)] + "/files/upload"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _uses_s3_upload(self) -> bool:
        return getattr(self, "_upload_backend", "http") in {"s3", "auto"}

    def _allows_http_upload_fallback(self) -> bool:
        return getattr(self, "_upload_backend", "http") == "auto"

    def _s3_store(self) -> S3UploadStore:
        store = getattr(self, "_s3_upload_store", None)
        if store is None:
            setup_error = getattr(self, "_s3_upload_setup_error", None)
            if setup_error is not None:
                raise RuntimeError(
                    f"YiCloud S3 upload backend setup failed: {setup_error}"
                ) from setup_error
            raise RuntimeError("YiCloud S3 upload backend is not configured")
        return store

    _S3_GUEST_PULL_MIN_FILE_BYTES = 1024 * 1024
    _S3_GUEST_PULL_EXEC_TIMEOUT_SEC = 600
    _S3_GUEST_PULL_PER_TRY_TIMEOUT_SEC = 150
    _S3_GUEST_PULL_DOWNLOAD_FAILED_EXIT = 123
    _S3_GUEST_PULL_MISSING_TOOL_EXIT = 125
    _S3_GUEST_PULL_NO_TOOLS_MARKER = "HARBOR_S3_GUEST_PULL_NO_TOOLS"
    _S3_GUEST_PULL_PROXY_UNSET = (
        "env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u WGETRC "
        "-u http_proxy -u https_proxy -u all_proxy "
        "NO_PROXY='*' no_proxy='*'"
    )

    @staticmethod
    def _s3_guest_pull_enabled() -> bool:
        return os.environ.get("YICLOUD_SANDBOX_S3_GUEST_PULL", "1").strip() != "0"

    @classmethod
    def _has_no_tools_marker(cls, result: object) -> bool:
        combined = f"{getattr(result, 'stderr', '') or ''}\n{getattr(result, 'stdout', '') or ''}"
        return cls._S3_GUEST_PULL_NO_TOOLS_MARKER in combined

    @classmethod
    def _build_s3_guest_pull_command(
        cls,
        *,
        kind: str,
        download_url: str,
        payload_digest: str,
        temp_path: str,
        target: str,
        mode_or_compression: str,
        per_try_timeout_sec: int = _S3_GUEST_PULL_PER_TRY_TIMEOUT_SEC,
    ) -> str:
        """Invoke scripts/s3_guest_pull.sh under bash strict mode with positional arguments."""
        if not payload_digest:
            raise RuntimeError("S3 artifact is missing payload_digest")
        script = _load_s3_guest_pull_script()
        launcher = (
            "if command -v bash >/dev/null 2>&1; then "
            'script="$1"; shift; exec bash -c "$script" s3-guest-pull "$@"; '
            "fi; "
            f"printf '%s\\n' {shlex.quote(cls._S3_GUEST_PULL_NO_TOOLS_MARKER)} >&2; "
            f"exit {cls._S3_GUEST_PULL_MISSING_TOOL_EXIT}"
        )
        quoted_args = " ".join(
            shlex.quote(str(arg))
            for arg in (
                script,
                kind,
                download_url,
                payload_digest,
                temp_path,
                target,
                mode_or_compression,
                per_try_timeout_sec,
            )
        )
        return (
            f"{cls._S3_GUEST_PULL_PROXY_UNSET} "
            f"sh -c {shlex.quote(launcher)} s3-guest-pull-bootstrap {quoted_args}"
        )

    async def _cleanup_guest_temp_payload(self, temp_path: str) -> None:
        try:
            cleanup = await self.exec(
                f"rm -f -- {shlex.quote(temp_path)}",
                cwd="/",
                timeout_sec=30,
                user="root",
            )
            if cleanup.return_code != 0:
                self.logger.warning("Failed to remove temporary S3 guest payload")
        except Exception:
            self.logger.warning(
                "Failed to remove temporary S3 guest payload", exc_info=True
            )

    async def _reverify_s3_source(self, kind: str) -> object | None:
        source = _ACTIVE_S3_STAGE_SOURCE.get()
        if source is None:
            return None
        store = getattr(self, "_s3_upload_store", None)
        if store is None:
            return None
        invalidate = getattr(store, "invalidate_cached_source", None)
        if not callable(invalidate):
            return None
        invalidate(kind, source)
        stage_fn = store.stage_directory if kind == "directory" else store.stage_file
        try:
            return await asyncio.to_thread(stage_fn, source)
        except Exception as exc:
            if not self._allows_http_upload_fallback():
                raise
            self.logger.warning(
                "YiCloud S3 stage re-verification failed backend=auto kind=%s error=%s",
                kind,
                exc,
            )
            return None

    async def _run_s3_guest_pull_attempt(
        self, command: str, temp_path: str
    ) -> object:
        """Execute one in-guest S3 pull attempt; propagate ambiguous exec exceptions without replay."""
        result = await self.exec(
            command,
            cwd="/",
            timeout_sec=self._S3_GUEST_PULL_EXEC_TIMEOUT_SEC,
            user="root",
        )
        if result.return_code != 0 and not (
            result.return_code == self._S3_GUEST_PULL_MISSING_TOOL_EXIT
            and self._has_no_tools_marker(result)
        ):
            await self._cleanup_guest_temp_payload(temp_path)
        return result

    async def _materialize_s3_file(self, artifact, target_path, mode):
        download_url = str(getattr(artifact, "download_url", "") or "").strip()
        payload_size = int(getattr(artifact, "payload_size", 0) or 0)
        if (
            download_url
            and payload_size >= self._S3_GUEST_PULL_MIN_FILE_BYTES
            and self._s3_guest_pull_enabled()
        ):
            chmod_mode = mode.zfill(3)
            for attempt in range(2):
                temp_path = f"/tmp/harbor-s3-{uuid.uuid4().hex}.part"
                command = self._build_s3_guest_pull_command(
                    kind="file",
                    download_url=download_url,
                    payload_digest=str(getattr(artifact, "payload_digest", "") or "").strip(),
                    temp_path=temp_path,
                    target=target_path,
                    mode_or_compression=chmod_mode,
                )
                result = await self._run_s3_guest_pull_attempt(
                    command, temp_path
                )
                if result.return_code == 0:
                    self.logger.info(
                        "YiCloud S3 guest pull materialized target=%s bytes=%s",
                        target_path,
                        payload_size,
                    )
                    return
                if (
                    result.return_code == self._S3_GUEST_PULL_MISSING_TOOL_EXIT
                    and self._has_no_tools_marker(result)
                ):
                    self.logger.info(
                        "YiCloud S3 guest pull unavailable (exit=%s), falling back to host HTTP upload target=%s",
                        result.return_code,
                        target_path,
                    )
                    break
                if result.return_code == self._S3_GUEST_PULL_DOWNLOAD_FAILED_EXIT:
                    if attempt == 0:
                        refreshed = await self._reverify_s3_source("file")
                        if refreshed is not None:
                            artifact = refreshed
                            download_url = str(
                                getattr(artifact, "download_url", "") or ""
                            ).strip()
                            continue
                    if self._allows_http_upload_fallback():
                        self.logger.warning(
                            "YiCloud S3 guest pull download failed (exit=%s), "
                            "falling back to host HTTP upload backend=auto target=%s",
                            result.return_code,
                            target_path,
                        )
                        break
                raise RuntimeError(
                    f"failed to materialize S3 file to {target_path!r} via guest pull: "
                    f"return_code={result.return_code}; "
                    f"{(result.stderr or result.stdout or 'no output')[-2000:]}"
                )
        await self._upload_file_http(Path(artifact.local_payload_path), target_path, mode)

    async def _extract_uploaded_directory(self, archive: Path, target_dir: str, *, gzip: bool) -> None:
        """Transport bytes through the file API, then separately extract them."""
        remote = f"/tmp/harbor-upload-{uuid.uuid4().hex}.tar"
        try:
            await self._upload_file_http(archive, remote, "600")
            result = await self.exec(
                f"mkdir -p {shlex.quote(target_dir)} && "
                f"tar {'xzf' if gzip else 'xf'} {shlex.quote(remote)} -C {shlex.quote(target_dir)}",
                cwd="/", timeout_sec=300, user="root",
            )
            if result.return_code != 0:
                raise RuntimeError(f"failed to upload directory to {target_dir!r}")
        finally:
            try:
                cleanup = await self.exec(f"rm -f -- {shlex.quote(remote)}", cwd="/", timeout_sec=30, user="root")
                if cleanup.return_code != 0:
                    self.logger.warning("Failed to remove temporary upload archive")
            except Exception:
                self.logger.warning("Failed to remove temporary upload archive", exc_info=True)

    async def _materialize_s3_directory(self, artifact, target_dir):
        is_gzip = getattr(artifact, "compression", "none") == "gzip"
        download_url = str(getattr(artifact, "download_url", "") or "").strip()
        if download_url and self._s3_guest_pull_enabled():
            compression = "gzip" if is_gzip else "none"
            for attempt in range(2):
                temp_path = f"/tmp/harbor-s3-{uuid.uuid4().hex}.tar"
                command = self._build_s3_guest_pull_command(
                    kind="directory",
                    download_url=download_url,
                    payload_digest=str(getattr(artifact, "payload_digest", "") or "").strip(),
                    temp_path=temp_path,
                    target=target_dir,
                    mode_or_compression=compression,
                )
                result = await self._run_s3_guest_pull_attempt(
                    command, temp_path
                )
                if result.return_code == 0:
                    self.logger.info(
                        "YiCloud S3 guest pull materialized target=%s bytes=%s",
                        target_dir,
                        getattr(artifact, "payload_size", 0),
                    )
                    return
                if (
                    result.return_code == self._S3_GUEST_PULL_MISSING_TOOL_EXIT
                    and self._has_no_tools_marker(result)
                ):
                    self.logger.info(
                        "YiCloud S3 guest pull unavailable (exit=%s), falling back to host HTTP upload target=%s",
                        result.return_code,
                        target_dir,
                    )
                    break
                if result.return_code == self._S3_GUEST_PULL_DOWNLOAD_FAILED_EXIT:
                    if attempt == 0:
                        refreshed = await self._reverify_s3_source("directory")
                        if refreshed is not None:
                            artifact = refreshed
                            is_gzip = getattr(artifact, "compression", "none") == "gzip"
                            compression = "gzip" if is_gzip else "none"
                            download_url = str(
                                getattr(artifact, "download_url", "") or ""
                            ).strip()
                            continue
                    if self._allows_http_upload_fallback():
                        self.logger.warning(
                            "YiCloud S3 guest pull download failed (exit=%s), "
                            "falling back to host HTTP upload backend=auto target=%s",
                            result.return_code,
                            target_dir,
                        )
                        break
                raise RuntimeError(
                    f"failed to materialize S3 directory to {target_dir!r} via guest pull: "
                    f"return_code={result.return_code}; "
                    f"{(result.stderr or result.stdout or 'no output')[-2000:]}"
                )
        await self._extract_uploaded_directory(
            Path(artifact.local_payload_path),
            target_dir,
            gzip=is_gzip,
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        if not source.is_file():
            raise RuntimeError(f"upload source is not a file: {source}")
        mode = f"{stat.S_IMODE(source.stat().st_mode):o}"
        if self._uses_s3_upload():
            started = time.monotonic()
            phase = "stage"
            try:
                artifact = await asyncio.to_thread(self._s3_store().stage_file, source)
                phase = "materialize"
                token = _ACTIVE_S3_STAGE_SOURCE.set(source)
                try:
                    await self._materialize_s3_file(artifact, target_path, mode)
                finally:
                    _ACTIVE_S3_STAGE_SOURCE.reset(token)
            except Exception as exc:
                if phase != "stage" or not self._allows_http_upload_fallback():
                    raise
                self.logger.warning(
                    "YiCloud upload fallback backend=auto "
                    "attempted_backend=s3 fallback_backend=http kind=file "
                    "phase=%s target=%s error=%s",
                    phase,
                    target_path,
                    exc,
                )
            else:
                elapsed = max(time.monotonic() - started, 0.001)
                self.logger.info(
                    "YiCloud upload complete backend=s3 kind=file "
                    "target=%s digest=%s size_bytes=%s elapsed_seconds=%.3f",
                    target_path,
                    artifact.logical_digest,
                    artifact.payload_size,
                    elapsed,
                )
                return
        await self._upload_file_http(source, target_path, mode)

    async def _upload_file_http(
        self, source: Path, target_path: str, mode: str
    ) -> None:
        await await_owned(asyncio.to_thread(self._upload_file_native_sync, source, target_path, mode))

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        if not source.is_dir():
            raise RuntimeError(f"upload source is not a directory: {source}")
        if (
            target_dir.rstrip("/") == "/logs/agent"
            and (
                getattr(self, "_oracle_skip_agent_log_reupload", False)
                or getattr(self, "_oracle_skip_agent_log_transfer", False)
            )
        ):
            self.logger.info(
                "YiCloud upload skipped purpose=oracle-agent-log-reupload "
                "target=%s",
                target_dir,
            )
            return
        if (
            target_dir.rstrip("/") == "/solution"
            and getattr(
                self,
                "_oracle_solution_upload_backend",
                "inherit",
            )
            == "inline"
        ):
            self.logger.warning("Oracle inline upload is deprecated; using native file delivery")
            await self._upload_dir_http(source, target_dir)
            return
        if (
            target_dir.rstrip("/") == "/solution"
            and getattr(
                self,
                "_oracle_solution_upload_backend",
                "inherit",
            )
            == "execd"
        ):
            started = time.monotonic()
            await self._upload_dir_http(source, target_dir)
            elapsed = max(time.monotonic() - started, 0.001)
            self.logger.info(
                "YiCloud upload complete backend=execd kind=directory "
                "purpose=oracle-solution target=%s elapsed_seconds=%.3f",
                target_dir,
                elapsed,
            )
            return
        if self._uses_s3_upload():
            started = time.monotonic()
            phase = "stage"
            try:
                artifact = await asyncio.to_thread(
                    self._s3_store().stage_directory, source
                )
                phase = "materialize"
                token = _ACTIVE_S3_STAGE_SOURCE.set(source)
                try:
                    await self._materialize_s3_directory(artifact, target_dir)
                finally:
                    _ACTIVE_S3_STAGE_SOURCE.reset(token)
            except Exception as exc:
                if phase != "stage" or not self._allows_http_upload_fallback():
                    raise
                self.logger.warning(
                    "YiCloud upload fallback backend=auto "
                    "attempted_backend=s3 fallback_backend=http "
                    "kind=directory phase=%s target=%s error=%s",
                    phase,
                    target_dir,
                    exc,
                )
            else:
                elapsed = max(time.monotonic() - started, 0.001)
                self.logger.info(
                    "YiCloud upload complete backend=s3 kind=directory "
                    "target=%s digest=%s payload_bytes=%s compression=%s "
                    "elapsed_seconds=%.3f",
                    target_dir,
                    artifact.logical_digest,
                    artifact.payload_size,
                    artifact.compression,
                    elapsed,
                )
                return
        await self._upload_dir_http(source, target_dir)

    async def _upload_dir_http(self, source: Path, target_dir: str) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive = Path(tmp_dir) / "upload.tar.gz"
            if (
                os.environ.get("HARBOR_NATIVE_CONCURRENCY") == "1"
                and os.environ.get("HARBOR_OPENSANDBOX_COMMAND_MODE", "sync")
                == "async"
            ):
                await pack_directory_async(source, archive)
            else:
                pack_directory(source, archive)
            await self._extract_uploaded_directory(archive, target_dir, gzip=True)

    async def _materialize_read_only_mounts(self) -> None:
        """Copy immutable host bind mounts into a non-mounted Sandbox."""
        for mount in getattr(self, "_mounts", []):
            if mount.get("type") != "bind" or not mount.get("read_only"):
                continue

            source_value = str(mount.get("source") or "").strip()
            target = str(mount.get("target") or "").strip()
            if not source_value or not target:
                raise RuntimeError(
                    "read-only OpenSandbox bind mounts require source and target"
                )
            if not target.startswith("/"):
                raise RuntimeError(
                    f"OpenSandbox mount target must be absolute: {target!r}"
                )

            source = Path(source_value)
            if not source.exists():
                raise RuntimeError(f"OpenSandbox mount source does not exist: {source}")

            self.logger.info(
                "Materializing read-only mount source=%s target=%s",
                source,
                target,
            )
            if source.is_dir():
                await self.upload_dir(source, target)
            elif source.is_file():
                await self.upload_file(source, target)
            else:
                raise RuntimeError(
                    f"unsupported OpenSandbox mount source type: {source}"
                )

    async def _download_file_chunks(
        self, source_path: str, target_path: Path | str
    ) -> None:
        await await_owned(asyncio.to_thread(self._download_file_native_sync, source_path, target_path))

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if (
            source_path.rstrip("/") == "/logs/agent/oracle.txt"
            and getattr(self, "_oracle_skip_agent_log_transfer", False)
        ):
            self.logger.info(
                "YiCloud download skipped purpose=oracle-agent-log "
                "source=%s",
                source_path,
            )
            return
        async with artifact_download_slot():
            await self._download_file_chunks(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if (
            source_dir.rstrip("/") == "/logs/agent"
            and getattr(self, "_oracle_skip_agent_log_transfer", False)
        ):
            self.logger.info(
                "YiCloud download skipped purpose=oracle-agent-log-directory "
                "source=%s",
                source_dir,
            )
            return
        await self._download_dir_archive(source_dir, target_dir, exclude=[])

    async def download_dir_with_exclusions(
        self,
        *,
        source_dir: str,
        target_dir: Path | str,
        exclude: list[str],
    ) -> None:
        await self._download_dir_archive(source_dir, target_dir, exclude=exclude)

    async def _download_dir_archive(
        self,
        source_dir: str,
        target_dir: Path | str,
        *,
        exclude: list[str],
    ) -> None:
        async with artifact_download_slot():
            remote_archive = f"/tmp/harbor-download-{uuid.uuid4().hex}.tar.gz"
            archive_q = shlex.quote(remote_archive)
            exclude_flags = " ".join(
                f"--exclude={shlex.quote(item)}" for item in exclude
            )
            try:
                result = await self.exec(
                    f"tar czf {archive_q} {exclude_flags} -C {shlex.quote(source_dir)} .",
                    cwd="/",
                    timeout_sec=300,
                    user="root",
                )
                if result.return_code != 0:
                    raise RuntimeError(
                        f"failed to archive directory {source_dir!r}: "
                        f"return_code={result.return_code}; "
                        f"{(result.stderr or result.stdout or 'no output')[-2000:]}"
                    )
                target = Path(target_dir)
                target.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory() as tmp_dir:
                    archive = Path(tmp_dir) / "download.tar.gz"
                    await self._download_file_chunks(remote_archive, archive)
                    with tarfile.open(archive, "r:gz") as tar:
                        tar.extractall(target, filter="data")
            finally:
                try:
                    cleanup = await self.exec(
                        f"rm -f -- {archive_q}",
                        cwd="/",
                        timeout_sec=30,
                        user="root",
                    )
                    if cleanup.return_code != 0:
                        self.logger.warning(
                            "Failed to remove temporary download archive"
                        )
                except Exception:
                    self.logger.warning(
                        "Failed to remove temporary download archive", exc_info=True
                    )
