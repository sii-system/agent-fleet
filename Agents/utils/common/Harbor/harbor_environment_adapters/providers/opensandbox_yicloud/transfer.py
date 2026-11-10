"""YiCloud artifact preparation around native host-initiated file transfers.

Packing/extraction may run in the task; payload transport never uses exec.
"""

from __future__ import annotations

import asyncio
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

    async def _materialize_s3_file(self, artifact, target_path, mode):
        # Staging already owns a verified local payload snapshot. Deliver those
        # bytes through the file API rather than starting a guest downloader.
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
        await self._extract_uploaded_directory(Path(artifact.local_payload_path), target_dir, gzip=False)

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
                await self._materialize_s3_file(artifact, target_path, mode)
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
                await self._materialize_s3_directory(artifact, target_dir)
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
