"""Real HTTP regressions for native binary transfer without guest commands."""

import asyncio
import errno
import hashlib
import hmac
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from urllib3.util import connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.opensandbox_yicloud.client import (
    YiCloudClientMixin,
)
from harbor_environment_adapters.providers.opensandbox_yicloud.native_files import (
    FILE_CHUNK_BYTES,
)
from harbor_environment_adapters.providers.opensandbox_yicloud.transfer import (
    YiCloudTransferMixin,
)


class FileProbe(YiCloudTransferMixin, YiCloudClientMixin):
    async def exec(self, *args, **kwargs):
        raise AssertionError("File transport must not execute a guest command")


class NativeFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.files = {}
        self.uploads = []
        self.read_sizes = []
        self.failure = None
        self.upload_entered = threading.Event()
        self.upload_release = threading.Event()
        self.settings = patch.dict(os.environ, {
            "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": "",
            "HARBOR_ARTIFACT_LOCK_DIR": str(self.root / "locks"),
        })
        self.settings.start()
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, payload, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                prefix = self.headers["X-OGW-TICK"] + "public"
                expected = hmac.new(b"private", prefix.encode() + body, hashlib.sha256).hexdigest()
                if self.headers["X-OGW-SIGN"] != expected:
                    self.reply(401, b"bad raw signature")
                    return
                message = BytesParser(policy=policy.default).parsebytes(
                    f'Content-Type: {self.headers["Content-Type"]}\r\n\r\n'.encode() + body)
                parts = {part.get_param("name", header="content-disposition"): part
                         for part in message.iter_parts()}
                metadata = json.loads(parts["metadata"].get_payload(decode=True))
                payload = parts["file"].get_payload(decode=True)
                probe.uploads.append((metadata, payload))
                if probe.failure == "upload-disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if probe.failure == "upload-blocked":
                    probe.upload_entered.set()
                    probe.upload_release.wait(5)
                if probe.failure == "upload-error":
                    self.reply(503, b"do not replay", {"Retry-After": "1"})
                    return
                probe.files[metadata["path"]] = payload
                self.reply(200, b"{}")

            def do_GET(self):
                url = urlsplit(self.path)
                path = parse_qs(url.query)["path"][0]
                payload = probe.files[path]
                if url.path.endswith("/files/info"):
                    metadata = {"size": len(payload), "modified": 1}
                    if probe.failure == "metadata-changed" and probe.read_sizes:
                        metadata["modified"] = 2
                    self.reply(200, json.dumps({path: metadata}).encode())
                    return
                offset, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
                data = payload[offset:end + 1]
                probe.read_sizes.append(len(data))
                if probe.failure == "oversized":
                    self.reply(200, payload)
                elif probe.failure == "short":
                    self.reply(206, data[:-1], {"Content-Range": f"bytes {offset}-{end}/{len(payload)}"})
                elif probe.failure == "wrong-range":
                    self.reply(206, data, {"Content-Range": f"bytes 1-2/{len(payload)}"})
                else:
                    self.reply(206, data, {"Content-Range": f"bytes {offset}-{end}/{len(payload)}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = FileProbe()
        self.env._sandbox_id = "sbx-test"
        self.env._command_url = f"http://127.0.0.1:{self.server.server_port}/proxy/44772/command"
        self.env._access_token = "sandbox-token"
        self.env._request_timeout_sec = 2
        from yicloud.base.auth.credential import Credential
        self.env._base_client = SimpleNamespace(crede=Credential("public", "private"))

    async def asyncTearDown(self):
        self.upload_release.set()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()
        self.settings.stop()
        self.tmp.cleanup()

    async def test_raw_signed_binary_roundtrip_larger_than_old_chunk_and_modes(self):
        payload = bytes(range(256)) * 4097 + b"\x00\xff\r\n"
        source = self.root / "source"
        source.write_bytes(payload)
        source.chmod(0o751)
        target = self.root / "download"
        target.write_bytes(b"old")
        target.chmod(0o640)
        remote = "/nested dir/new/file ' name"
        await self.env.upload_file(source, remote)
        await self.env.download_file(remote, target)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][0]["mode"], 751)
        self.assertEqual(self.uploads[0][1], payload)
        self.assertLessEqual(max(self.read_sizes), FILE_CHUNK_BYTES)

    async def test_empty_file_and_no_remote_exec(self):
        source = self.root / "empty"
        source.touch()
        await self.env.upload_file(source, "/empty")
        await self.env.download_file("/empty", self.root / "out")
        self.assertEqual((self.root / "out").read_bytes(), b"")
        self.assertFalse(self.read_sizes)

    async def test_bad_ranges_and_changed_metadata_preserve_existing_target(self):
        self.files["/remote"] = os.urandom(FILE_CHUNK_BYTES + 73)
        target = self.root / "existing"
        for failure in ("short", "wrong-range", "oversized", "metadata-changed"):
            with self.subTest(failure=failure):
                target.write_bytes(b"previous valid target")
                self.failure = failure
                self.read_sizes.clear()
                with self.assertRaises(RuntimeError):
                    await self.env.download_file("/remote", target)
                self.assertEqual(target.read_bytes(), b"previous valid target")
                self.assertFalse(list(self.root.glob(".harbor-download-*.part")))

    async def test_failed_upload_is_not_replayed(self):
        self.failure = "upload-error"
        source = self.root / "source"
        source.write_bytes(b"payload")
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            await self.env.upload_file(source, "/remote")
        self.assertEqual(len(self.uploads), 1)

    async def test_connection_refusal_retries_before_sending_one_binary_body(self):
        original = connection.create_connection
        attempts = []

        def initially_refused(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(errno.ECONNREFUSED, "test connection refused before request")
            return original(*args, **kwargs)

        source = self.root / "source"
        source.write_bytes(bytes(range(256)) * 4097)
        with patch.object(connection, "create_connection", initially_refused):
            await self.env.upload_file(source, "/remote")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.files["/remote"], source.read_bytes())

    async def test_connection_retries_are_bounded(self):
        source = self.root / "source"
        source.write_bytes(b"payload")
        with patch.object(connection, "create_connection", side_effect=OSError(
            errno.ECONNREFUSED, "test connection refused before request"
        )) as connect, self.assertRaises(requests.ConnectionError):
            await self.env.upload_file(source, "/remote")
        self.assertEqual(connect.call_count, 3)
        self.assertFalse(self.uploads)

    async def test_disconnect_after_server_reads_body_is_not_replayed(self):
        self.failure = "upload-disconnect"
        source = self.root / "source"
        source.write_bytes(b"payload")
        with self.assertRaises(requests.ConnectionError):
            await self.env.upload_file(source, "/remote")
        self.assertEqual(len(self.uploads), 1)

    async def test_cancel_waits_for_owned_upload_worker(self):
        self.failure = "upload-blocked"
        source = self.root / "source"
        source.write_bytes(b"payload")
        operation = asyncio.create_task(self.env.upload_file(source, "/remote"))
        self.assertTrue(await asyncio.to_thread(self.upload_entered.wait, 3))
        operation.cancel()
        await asyncio.sleep(0)
        self.assertFalse(operation.done())
        self.upload_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(self.files["/remote"], b"payload")

    def test_binary_hmac_matches_sdk_for_utf8_and_keeps_query_protocol(self):
        from yicloud.base.auth.sign import parse_and_sort_query
        body = io.BytesIO(b"utf8 text")
        url = self.env._command_url + "?z=2&a=hello%20world&a=x"
        headers = self.env._multipart_headers(body, url, "multipart/form-data", signed=True)
        prefix = (headers["X-OGW-TICK"] + "public" + parse_and_sort_query(urlsplit(url).query)).encode()
        self.assertEqual(headers["X-OGW-SIGN"], hmac.new(b"private", prefix + body.read(), hashlib.sha256).hexdigest())


if __name__ == "__main__":
    unittest.main()
