"""Host-side binary file transport through YiCloud's native execd file APIs.

Guest processes never move payload bytes. SDK 0.4.1 signs UTF-8 text only;
raw multipart bodies use its HMAC protocol over bytes, streamed on the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from urllib3.util.retry import Retry

from .config import _positive_int

FILE_CHUNK_BYTES = 512 * 1024
METADATA_LIMIT_BYTES = 64 * 1024


def _read_bounded(response, maximum):
    """Bound responses even when the server ignores Range or sends no length."""
    data = bytearray()
    for chunk in response.iter_content(min(maximum + 1, 64 * 1024)):
        data.extend(chunk)
        if len(data) > maximum:
            raise RuntimeError("YiCloud file response exceeds requested limit")
    return bytes(data)


class YiCloudNativeFilesMixin:
    """Native file requests sharing the adapter's fixed Sandbox identity."""

    def _file_url(self, suffix, path):
        url = self._execd_url(suffix)
        return url + ("&" if "?" in url else "?") + urlencode({"path": path})

    def _multipart_headers(self, body, url, content_type, *, signed):
        if not signed:
            return {"X-Sandbox-Access-Token": self._access_token,
                    "Content-Type": content_type, "Accept": "application/json"}
        # SDK Credential.sign decodes bytes as UTF-8. Keep its timestamp/query
        # protocol, but hash raw body bytes so arbitrary binary files survive.
        from yicloud.base.auth.sign import parse_and_sort_query

        headers = self._signed_headers("", url=url, content_type=content_type,
                                       accept="application/json")
        credential = self._base_client.crede
        prefix = (headers["X-OGW-TICK"] + credential.public_key
                  + parse_and_sort_query(urlsplit(url).query)).encode("utf-8")
        digest = hmac.new(credential.private_key.encode("utf-8"), prefix, hashlib.sha256)
        body.seek(0)
        while chunk := body.read(FILE_CHUNK_BYTES):
            digest.update(chunk)
        body.seek(0)
        headers["X-OGW-SIGN"] = digest.hexdigest()
        return headers

    def _upload_file_native_sync(self, source, target_path, mode):
        """Retry connection establishment only; never replay a partial upload."""
        if not target_path.startswith("/") or any(c in target_path for c in "\x00\r\n"):
            raise ValueError("YiCloud upload target must be an absolute path")
        timeout = _positive_int(os.environ.get("YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC", "1800"),
                                "YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC")
        upload_url = self._fast_upload_url() or self._execd_url("/files/upload")
        signed = not os.environ.get("YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", "").strip()
        boundary = "harbor-" + uuid.uuid4().hex
        metadata = json.dumps({"path": target_path, "owner": "root", "group": "root",
                               "mode": int(mode)}, separators=(",", ":")).encode()
        with tempfile.TemporaryFile(prefix="yicloud-upload-") as body:
            body.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"; "
                        "filename=\"metadata.json\"\r\nContent-Type: application/json\r\n\r\n").encode())
            body.write(metadata)
            body.write((f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                        "filename=\"payload\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
            with Path(source).open("rb") as incoming:
                shutil.copyfileobj(incoming, body, FILE_CHUNK_BYTES)
            body.write(f"\r\n--{boundary}--\r\n".encode())
            size = body.tell()
            body.seek(0)
            headers = self._multipart_headers(body, upload_url,
                                             f"multipart/form-data; boundary={boundary}", signed=signed)
            headers["Content-Length"] = str(size)
            with requests.Session() as session:
                session.trust_env = False
                # urllib3 classifies DNS/refused/connect-timeout failures before
                # sending request bytes. Read/protocol errors and HTTP responses
                # can follow remote writes, so they must never trigger a replay.
                session.mount(upload_url, requests.adapters.HTTPAdapter(max_retries=Retry(
                    total=2, connect=2, read=0, status=0, other=0, redirect=0,
                    allowed_methods={"POST"}, backoff_factor=0.2,
                    respect_retry_after_header=False,
                )))
                with session.post(upload_url, data=body, headers=headers, stream=True,
                                  timeout=(20, timeout), allow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        # Never include response bodies, signed URLs or credentials.
                        raise RuntimeError(f"YiCloud file upload failed: HTTP {response.status_code}")
                    _read_bounded(response, METADATA_LIMIT_BYTES)

    def _file_get(self, session, suffix, path, *, maximum, extra_headers=None):
        url = self._file_url(suffix, path)
        headers = self._signed_headers("", url=url, accept="application/octet-stream")
        headers.update(extra_headers or {})
        with session.get(url, headers=headers, stream=True, timeout=(20, self._request_timeout_sec),
                         allow_redirects=False) as response:
            if response.status_code not in {200, 206}:
                raise RuntimeError(f"YiCloud file read failed: HTTP {response.status_code}")
            return response.status_code, response.headers, _read_bounded(response, maximum)

    def _file_info(self, session, path):
        _, _, payload = self._file_get(session, "/files/info", path,
                                       maximum=METADATA_LIMIT_BYTES)
        envelope = json.loads(payload)
        data = envelope.get(path) if isinstance(envelope, dict) else None
        if not isinstance(data, dict) or type(data.get("size")) is not int or data["size"] < 0:
            raise RuntimeError("YiCloud file metadata has no valid size")
        return data

    def _download_file_native_sync(self, source_path, target_path):
        """Validate ranges and source metadata before replacing a host target."""
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        with requests.Session() as session:
            session.trust_env = False
            before = self._file_info(session, source_path)
            size = before["size"]
            descriptor, name = tempfile.mkstemp(prefix=".harbor-download-", suffix=".part", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    if target.exists():
                        os.fchmod(output.fileno(), stat.S_IMODE(target.stat().st_mode))
                    for offset in range(0, size, FILE_CHUNK_BYTES):
                        end = min(offset + FILE_CHUNK_BYTES, size) - 1
                        status, headers, data = self._file_get(
                            session, "/files/download", source_path, maximum=end - offset + 1,
                            extra_headers={"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity"})
                        if (status != 206 or headers.get("Content-Range") != f"bytes {offset}-{end}/{size}"
                                or len(data) != end - offset + 1):
                            raise RuntimeError("YiCloud file Range mismatch or source changed")
                        output.write(data)
                    if self._file_info(session, source_path) != before:
                        raise RuntimeError("YiCloud download source changed during transfer")
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
