"""Transfer bounded archives and stat probes through sii-runfield without guest tools."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

MAX_ARCHIVE_BYTES = 512 << 20
ARCHIVE_MEDIA_TYPE = "application/x-tar"


class RunfieldArchiveClientMixin:
    """Delegate container-external stat and single-attempt tar streams to sii-runfield."""

    async def stat_path(self, sandbox_id: str, path: str) -> dict[str, Any]:
        """Return container-external path metadata without starting an execution."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            stat = await sbx.stat(path, timeout_seconds=self.request_timeout_sec)
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error
        if not isinstance(stat, dict) or not isinstance(stat.get("exists"), bool):
            raise TypeError("Runfield Controller response is missing stat metadata")
        return dict(stat)

    async def upload_archive(self, sandbox_id: str, path: str, archive: Path) -> None:
        """Send one archive once; never fall back to exec or replay partial writes."""
        archive_path = Path(archive)
        size = archive_path.stat().st_size
        if not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ValueError("Runfield archive upload exceeds protocol limit")
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            await sbx.upload_archive(
                path, archive_path, timeout_seconds=self.transfer_timeout_sec
            )
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error

    async def download_archive(
        self, sandbox_id: str, path: str, archive: Path, maximum_bytes: int
    ) -> None:
        """Save only on the host; oversized or failed streams remove owned staging."""
        archive_path = Path(archive)
        if os.path.lexists(archive_path):
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), str(archive_path)
            )
        limit = min(maximum_bytes, MAX_ARCHIVE_BYTES)
        if limit <= 0:
            raise ValueError("Runfield archive download exceeds configured limit")
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            size = await sbx.download_archive(
                path, dest=archive_path, timeout_seconds=self.transfer_timeout_sec
            )
            if not isinstance(size, int) or isinstance(size, bool):
                size = archive_path.stat().st_size if archive_path.exists() else 0
            if size > limit:
                raise ValueError("Runfield archive download exceeds configured limit")
        except sdk_error_cls as error:
            archive_path.unlink(missing_ok=True)
            if getattr(error, "code", None) == "resource_limit_exceeded":
                raise ValueError(
                    "Runfield archive download exceeds configured limit"
                ) from error
            raise self._translate_sdk_error(error) from error
        except BaseException:
            archive_path.unlink(missing_ok=True)
            raise
