"""WAA guest HTTP contract; these tests do not emulate Windows processes."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kubevirt_windows import transport


class WAATransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(hasattr(transport, "WAATransport"), "WAA HTTP transport is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.guest = transport.WAATransport(
            SimpleNamespace(waa_port=5000, transfer_timeout=30), "trial", "192.0.2.10",
            self.root,
        )
        self.addAsyncCleanup(self.guest.close)
        self.requests = []

        def handle(request):
            self.requests.append(request)
            if request.url.path == "/probe":
                return httpx.Response(200, json={"status": "Probe successful"})
            if request.url.path == "/execute":
                return httpx.Response(200, json={
                    "status": "success", "output": base64.b64encode("任务✓".encode()).decode(),
                    "error": "", "returncode": 0,
                })
            if request.url.path == "/setup/upload":
                return httpx.Response(200, text="File Uploaded")
            if request.url.path == "/file":
                return httpx.Response(200, content=b"\x00\xffbinary")
            raise AssertionError(request.url.path)

        await self.guest.close()
        self.guest.client = httpx.AsyncClient(
            base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handle),
        )

    async def test_powershell_uses_waa_argv_and_encoding_safe_output(self):
        self.assertEqual(await self.guest.powershell("Write-Output '任务✓'"), "任务✓")
        request = self.requests[-1]
        payload = json.loads(request.content)
        self.assertFalse(payload["shell"])
        self.assertEqual(payload["command"][0], "powershell.exe")
        script = base64.b64decode(payload["command"][-1]).decode("utf-16-le")
        self.assertIn("任务✓", script)
        self.assertIn("ToBase64String", script)
        self.assertNotIn("authorization", request.headers)

    async def test_probe_requires_waa_response(self):
        await self.guest.probe()
        self.assertEqual(self.requests[0].url.path, "/probe")
        self.guest.client = await self.replace_client(lambda r: httpx.Response(200, json={"status": "ok"}))
        with self.assertRaisesRegex(RuntimeError, "WAA"):
            await self.guest.probe()

    async def replace_client(self, handler):
        await self.guest.client.aclose()
        return httpx.AsyncClient(base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handler))

    async def test_upload_and_download_match_waa_form_contract(self):
        source = self.root / "binary.bin"
        source.write_bytes(b"\x00\xffbinary")
        await self.guest.upload_file(source, "C:/tools/任务.bin")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/setup/upload")
        self.assertIn(b'name="file_path"', request.content)
        self.assertIn(b'name="file_data"', request.content)
        self.assertIn(b"\x00\xffbinary", request.content)
        await self.guest.download_file("C:/tools/任务.bin", self.root / "out.bin")
        self.assertEqual(self.requests[-1].url.path, "/file")
        self.assertIn(b"file_path=", self.requests[-1].content)
        self.assertEqual((self.root / "out.bin").read_bytes(), source.read_bytes())

    async def test_guest_errors_do_not_echo_command_or_response_body(self):
        for response in [httpx.Response(500, text="fake-secret"), httpx.Response(200, json={
            "status": "success", "returncode": 1, "output": "", "error": "fake-secret",
        })]:
            self.guest.client = await self.replace_client(lambda r, response=response: response)
            with self.assertRaises(RuntimeError) as caught:
                await self.guest.powershell("fake-secret")
            self.assertNotIn("fake-secret", str(caught.exception))

    async def test_long_command_launches_then_polls_without_blocking_execute_endpoint(self):
        self.guest.upload_file = AsyncMock()
        result = {"stdout": "ok", "stderr": "", "return_code": 7, "timed_out": False}
        self.guest.powershell = AsyncMock(side_effect=["", "null", json.dumps(result)])
        actual = await self.guest.execute("echo task", cwd="C:/work", env={}, timeout=600)
        self.assertEqual(actual, result)
        calls = self.guest.powershell.await_args_list
        self.assertIn("Start-Process", calls[0].args[0])
        self.assertIn(".result", calls[-1].args[0])
        self.assertTrue(all(call.kwargs.get("timeout", 60) < 120 for call in calls))

    async def test_cancellation_signals_supervisor(self):
        self.guest.upload_file = AsyncMock()
        self.guest.powershell = AsyncMock(side_effect=["", asyncio.CancelledError(), ""])
        with self.assertRaises(asyncio.CancelledError):
            await self.guest.execute("task", cwd="C:/work", env={}, timeout=600)
        self.assertIn(".cancel", self.guest.powershell.await_args_list[-1].args[0])

    async def test_supervisor_error_is_reported_without_echoing_guest_data(self):
        self.guest.upload_file = AsyncMock()
        self.guest.powershell = AsyncMock(side_effect=["", '{"error":"fake-secret"}', ""])
        with self.assertRaises(RuntimeError) as caught:
            await self.guest.execute("task", cwd="C:/work", env={}, timeout=600)
        self.assertNotIn("fake-secret", str(caught.exception))

    async def test_control_response_is_bounded(self):
        self.guest.client = await self.replace_client(lambda r: httpx.Response(200, content=b"x" * (17 * 1024 * 1024)))
        with self.assertRaisesRegex(RuntimeError, "limit"):
            await self.guest.powershell("Write-Output x")


class WAALoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_multipart_and_binary_roundtrip(self):
        files = {}
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path, self.headers.get("Authorization")))
                if self.path == "/execute":
                    command = json.loads(body)
                    self.server.test.assertFalse(command["shell"])
                    self.server.test.assertEqual(command["command"][0], "powershell.exe")
                    data = json.dumps({
                        "status": "success", "returncode": 0, "error": "",
                        "output": base64.b64encode("任务".encode()).decode(),
                    }).encode()
                elif self.path == "/setup/upload":
                    envelope = ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode()
                    message = BytesParser(policy=default).parsebytes(envelope + body)
                    parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                             for part in message.iter_parts()}
                    files[parts["file_path"].decode()] = parts["file_data"]
                    data = b"File Uploaded"
                elif self.path == "/file":
                    path = parse_qs(body.decode())["file_path"][0]
                    data = files[path]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.test = self
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                guest = transport.WAATransport(
                    SimpleNamespace(waa_port=server.server_port, transfer_timeout=5),
                    "trial", "127.0.0.1", root,
                )
                try:
                    self.assertEqual(await guest.powershell("'任务'"), "任务")
                    source = root / "source.bin"
                    source.write_bytes(bytes(range(256)) * 4096)
                    await guest.upload_file(source, "C:/任务/file.bin")
                    await guest.download_file("C:/任务/file.bin", root / "out.bin")
                    self.assertEqual((root / "out.bin").read_bytes(), source.read_bytes())
                    self.assertEqual(files["C:/任务/file.bin"], source.read_bytes())
                    self.assertTrue(all(auth is None for _, auth in calls))
                finally:
                    await guest.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
