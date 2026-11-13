"""Stream bounded archives through Controller; never execute guest transfer tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from async_utils import await_owned

MAX_ARCHIVE_BYTES = 512 << 20
ARCHIVE_MEDIA_TYPE = "application/x-tar"


class DsecArchiveClientMixin:
    """Own binary HTTP transport; failed uploads may have partial side effects."""

    def _archive_url(self, sandbox_id: str, path: str) -> str:
        return (
            self.endpoint + f"/v1alpha1/sandboxes/{sandbox_id}/archive?"
            + urlencode({"path": path})
        )

    def _archive_stat_url(self, sandbox_id: str, path: str) -> str:
        return (
            self.endpoint + f"/v1alpha1/sandboxes/{sandbox_id}/archive/stat?"
            + urlencode({"path": path})
        )

    def _stat_path_sync(self, sandbox_id: str, path: str) -> dict[str, Any]:
        """Probe container path metadata outside guest execution via Controller stat."""
        response = self.session.request(
            "GET",
            self._archive_stat_url(sandbox_id, path),
            headers={"Accept": "application/json"},
            timeout=self.request_timeout_sec,
            allow_redirects=False,
            stream=True,
        )
        try:
            result = self._parse_response(response)
        finally:
            response.close()
        stat = result.get("stat")
        if not isinstance(stat, dict) or not isinstance(stat.get("exists"), bool):
            raise TypeError("DSec Controller response is missing stat metadata")
        return stat

    async def stat_path(self, sandbox_id: str, path: str) -> dict[str, Any]:
        """Return container-external path metadata without starting an execution."""
        return await await_owned(
            asyncio.to_thread(self._stat_path_sync, sandbox_id, path)
        )

    def _check_archive_response(self, response: Any) -> None:
        if response.status_code != 200:
            self._parse_response(response)
            raise RuntimeError("DSec Controller returned an unexpected archive status")

    def _upload_archive_sync(self, sandbox_id: str, path: str, archive: Path) -> None:
        size = archive.stat().st_size
        if not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ValueError("DSec archive upload exceeds protocol limit")
        with archive.open("rb") as source, self.session.request(
            "PUT", self._archive_url(sandbox_id, path), data=source,
            headers={"Content-Type": ARCHIVE_MEDIA_TYPE, "Content-Length": str(size)},
            timeout=self.transfer_timeout_sec, allow_redirects=False, stream=True,
        ) as response:
            self._check_archive_response(response)
            self._parse_response(response)

    async def upload_archive(self, sandbox_id: str, path: str, archive: Path) -> None:
        """Send one archive once; never fall back to exec or replay partial writes."""
        await await_owned(asyncio.to_thread(self._upload_archive_sync, sandbox_id, path, archive))

    def _download_archive_sync(
        self, sandbox_id: str, path: str, archive: Path, maximum_bytes: int
    ) -> None:
        with self.session.request(
            "GET", self._archive_url(sandbox_id, path),
            headers={"Accept": ARCHIVE_MEDIA_TYPE}, timeout=self.transfer_timeout_sec,
            allow_redirects=False, stream=True,
        ) as response:
            self._check_archive_response(response)
            if (
                response.headers.get("Content-Type") != ARCHIVE_MEDIA_TYPE
                or response.headers.get("Content-Encoding")
            ):
                raise RuntimeError("DSec archive response has an invalid media type")
            total = 0
            owned = False
            try:
                with archive.open("xb") as output:
                    owned = True
                    for chunk in response.iter_content(64 << 10):
                        total += len(chunk)
                        if total > min(maximum_bytes, MAX_ARCHIVE_BYTES):
                            raise ValueError("DSec archive download exceeds configured limit")
                        output.write(chunk)
            except BaseException:
                if owned:
                    archive.unlink(missing_ok=True)
                raise

    async def download_archive(
        self, sandbox_id: str, path: str, archive: Path, maximum_bytes: int
    ) -> None:
        """Save only on the host; truncated streams fail before extraction."""
        await await_owned(asyncio.to_thread(
            self._download_archive_sync, sandbox_id, path, archive, maximum_bytes
        ))
