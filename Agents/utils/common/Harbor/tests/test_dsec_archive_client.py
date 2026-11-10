"""Exercise native archive HTTP streaming, bounds, and host file ownership."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.dsec_controller.client import (
    DsecControllerClient,
)


class DsecArchiveClientTests(unittest.TestCase):
    def setUp(self):
        self.payload = b"\0\xffarchive bytes" * 10000
        self.received = b""
        self.response_type = "application/x-tar"
        self.truncated = False
        self.response_delay = 0
        self.block_response = False
        self.response_started = threading.Event()
        self.response_release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def wait_for_response(self):
                owner.response_started.set()
                if owner.block_response:
                    owner.response_release.wait(5)
                time.sleep(owner.response_delay)

            def do_PUT(self):
                owner.received = self.rfile.read(int(self.headers["Content-Length"]))
                self.wait_for_response()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"api_version":"sandbox-lifecycle/v1alpha1","status":"ok","result":{}}')

            def do_GET(self):
                self.wait_for_response()
                self.send_response(200)
                self.send_header("Content-Type", owner.response_type)
                self.send_header("Content-Length", str(len(owner.payload) + (1 if owner.truncated else 0)))
                self.end_headers()
                self.wfile.write(owner.payload)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = DsecControllerClient(
            f"http://127.0.0.1:{self.server.server_port}", allow_insecure_http=True
        )

    def tearDown(self):
        self.response_release.set()
        self.client.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_binary_upload_and_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.tar"
            source.write_bytes(self.payload)
            asyncio.run(self.client.upload_archive("sandbox-one", "/tests", source))
            self.assertEqual(self.received, self.payload)
            target = root / "download.tar"
            asyncio.run(self.client.download_archive("sandbox-one", "/tests", target, len(self.payload)))
            self.assertEqual(target.read_bytes(), self.payload)

    def test_truncated_and_oversized_downloads_remove_only_owned_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download.tar"
            with self.assertRaisesRegex(ValueError, "exceeds configured limit"):
                asyncio.run(self.client.download_archive("sandbox-one", "/tests", target, 1))
            self.assertFalse(target.exists())
            self.truncated = True
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                asyncio.run(self.client.download_archive("sandbox-one", "/tests", target, len(self.payload) + 1))
            self.assertFalse(target.exists())
            self.truncated = False
            target.write_bytes(b"keep existing file")
            with self.assertRaises(FileExistsError):
                asyncio.run(self.client.download_archive("sandbox-one", "/tests", target, len(self.payload)))
            self.assertEqual(target.read_bytes(), b"keep existing file")

    def test_wrong_media_type_is_rejected_before_creating_staging(self):
        self.response_type = "application/json"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download.tar"
            with self.assertRaisesRegex(RuntimeError, "invalid media type"):
                asyncio.run(self.client.download_archive("sandbox-one", "/tests", target, len(self.payload)))
            self.assertFalse(target.exists())

    def test_archive_timeout_is_independent_from_control_timeout(self):
        self.client.request_timeout_sec = 1
        self.client.transfer_timeout_sec = 3
        self.response_delay = 1.2
        self.test_binary_upload_and_download()

    def test_cancelled_transfers_keep_owned_paths_until_http_worker_finishes(self):
        async def exercise(upload):
            paths = []

            async def transfer_with_owned_path():
                with tempfile.TemporaryDirectory() as temporary:
                    archive = Path(temporary) / "data.tar"
                    paths.append(archive)
                    if upload:
                        archive.write_bytes(self.payload)
                        await self.client.upload_archive("sandbox-one", "/tests", archive)
                    else:
                        await self.client.download_archive("sandbox-one", "/tests", archive, len(self.payload))

            self.block_response = True
            self.response_started.clear()
            self.response_release.clear()
            operation = asyncio.create_task(transfer_with_owned_path())
            try:
                self.assertTrue(await asyncio.to_thread(self.response_started.wait, 3))
                operation.cancel()
                await asyncio.sleep(0)
                self.assertFalse(operation.done())
                self.assertTrue(paths[0].parent.exists())
            finally:
                self.response_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertFalse(paths[0].parent.exists())

        for upload in (True, False):
            with self.subTest(upload=upload):
                asyncio.run(exercise(upload))


if __name__ == "__main__":
    unittest.main()
