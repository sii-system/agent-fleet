"""ALE Windows wire contract; loopback tests do not emulate Windows execution."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kubevirt_windows.ale import ALETransport


def sse(result):
    return "data: " + json.dumps(result) + "\n\n"


class ALETransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.guest = ALETransport(
            SimpleNamespace(waa_port=5000, transfer_timeout=30), "trial",
            "192.0.2.10", self.root,
        )
        self.addAsyncCleanup(self.guest.close)
        self.requests = []

    async def respond(self, response):
        await self.guest.client.aclose()

        def handler(request):
            self.requests.append(request)
            return response

        self.guest.client = httpx.AsyncClient(
            base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handler),
        )

    async def test_readiness_requires_ale_status(self):
        await self.respond(httpx.Response(200, json={"status": "ok"}))
        await self.guest.probe()
        self.assertEqual(self.requests[-1].url.path, "/status")
        for body in ({"status": "Probe successful"}, [], {}, None):
            await self.respond(httpx.Response(200, content=json.dumps(body)))
            with self.assertRaisesRegex(RuntimeError, "ALE"):
                await self.guest.probe()

    async def test_powershell_uses_command_string_and_encoding_safe_sse(self):
        for wire in (
            sse({"success": True, "return_code": 0, "stdout": "5Lu75Yqh4pyT"}),
            ('\ufeff: heartbeat\r\nevent: result\r\ndata: {"success":true,\r\n'
             'data: "return_code":0,"stdout":"5Lu75Yqh4pyT"}\r\n\r\n'),
        ):
            await self.respond(httpx.Response(200, text=wire, headers={"content-type": "text/plain"}))
            self.assertEqual(await self.guest.powershell("Write-Output '任务✓'"), "任务✓")
            request = self.requests[-1]
            self.assertEqual(request.url.path, "/cmd")
            payload = json.loads(request.content)
            self.assertEqual(payload["command"], "run_command")
            command = payload["params"]["command"]
            self.assertTrue(command.startswith("powershell.exe "))
            decoded = base64.b64decode(command.split()[-1]).decode("utf-16-le")
            self.assertIn("任务✓", decoded)
            self.assertIn("ToBase64String", decoded)
            self.assertNotIn("authorization", request.headers)

    async def test_malformed_and_failed_commands_are_redacted_and_never_retried(self):
        for wire in (
            'data: {"success":false,"error":"fake-secret"}\n\n',
            sse({"success": True, "return_code": 7, "stdout": "", "stderr": "fake-secret"}),
            sse({"success": True, "return_code": 0, "stdout": "invalid!"}),
            sse({"success": True, "return_code": 0}),
            sse({"success": True, "return_code": False, "stdout": ""}),
            "data: fake-secret\n\n", "data: []\n\n", "{}", ": heartbeat\n\n",
        ):
            await self.respond(httpx.Response(200, text=wire))
            before = len(self.requests)
            with self.assertRaises(RuntimeError) as caught:
                await self.guest.powershell("fake-secret")
            self.assertNotIn("fake-secret", str(caught.exception))
            self.assertEqual(len(self.requests), before + 1)
        await self.respond(httpx.Response(500, text="fake-secret"))
        with self.assertRaises(RuntimeError) as caught:
            await self.guest.probe()
        self.assertNotIn("fake-secret", str(caught.exception))

    async def test_control_response_limit(self):
        await self.respond(httpx.Response(200, content=b"x" * (17 * 1024 * 1024)))
        with self.assertRaisesRegex(RuntimeError, "limit"):
            await self.guest.powershell("echo x")

    async def test_connection_failures_retry_before_sending_command(self):
        for failure in (httpx.ConnectError, httpx.ConnectTimeout):
            attempts = []

            def handler(request, attempts=attempts, failure=failure):
                attempts.append(request)
                if len(attempts) < 3:
                    raise failure("fake-secret", request=request)
                return httpx.Response(200, text=sse({"success": True, "return_code": 0, "stdout": ""}))

            await self.guest.client.aclose()
            self.guest.client = httpx.AsyncClient(
                base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handler),
            )
            with patch("kubevirt_windows.transport.asyncio.sleep", new_callable=AsyncMock):
                self.assertEqual(await self.guest.powershell("echo x"), "")
            self.assertEqual(len(attempts), 3)
            self.assertTrue(all(request.content == attempts[0].content for request in attempts))

    async def test_transport_failures_are_bounded_and_redacted(self):
        for failure, expected_attempts in (
            (httpx.ConnectError, 3), (httpx.ConnectTimeout, 3),
            (httpx.ReadError, 1), (httpx.ReadTimeout, 1),
            (httpx.WriteError, 1), (httpx.WriteTimeout, 1),
            (httpx.RemoteProtocolError, 1), (httpx.PoolTimeout, 1),
        ):
            attempts = []

            def handler(request, attempts=attempts, failure=failure):
                attempts.append(request)
                raise failure("fake-secret", request=request)

            await self.guest.client.aclose()
            self.guest.client = httpx.AsyncClient(
                base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handler),
            )
            with (patch("kubevirt_windows.transport.asyncio.sleep", new_callable=AsyncMock),
                  self.assertRaisesRegex(RuntimeError, failure.__name__) as caught):
                await self.guest.powershell("fake-secret")
            self.assertNotIn("fake-secret", str(caught.exception))
            self.assertEqual(len(attempts), expected_attempts)

    async def test_connection_retry_respects_original_deadline_and_cancellation(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            raise httpx.ConnectError("fake-secret", request=request)

        await self.guest.client.aclose()
        self.guest.client = httpx.AsyncClient(
            base_url="http://192.0.2.10:5000", transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(TimeoutError):
            await self.guest.powershell("echo x", timeout=0.02)
        self.assertEqual(len(attempts), 1)
        with (patch("kubevirt_windows.transport.asyncio.sleep", new_callable=AsyncMock,
                    side_effect=asyncio.CancelledError),
              self.assertRaises(asyncio.CancelledError)):
            await self.guest.powershell("echo x")
        self.assertEqual(len(attempts), 2)

    async def test_helper_failures_report_only_exit_code(self):
        for code, diagnostic in ((7, "return_code=7"), (None, "invalid return_code"),
                                 ("fake-secret", "invalid return_code")):
            await self.respond(httpx.Response(200, text=sse({
                "success": True, "return_code": code, "stdout": "fake-secret", "stderr": "fake-secret",
            })))
            with self.assertRaisesRegex(RuntimeError, diagnostic) as caught:
                await self.guest.powershell("fake-secret")
            self.assertNotIn("fake-secret", str(caught.exception))

    async def test_read_failure_after_partial_event_does_not_replay_command(self):
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"success":'
                raise httpx.ReadError("fake-secret")

        await self.respond(httpx.Response(200, stream=BrokenStream()))
        with self.assertRaisesRegex(RuntimeError, "ReadError") as caught:
            await self.guest.powershell("echo x")
        self.assertNotIn("fake-secret", str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_supervised_execution_preserves_request_data_and_exit_code(self):
        captured = {}

        async def upload(source, target):
            captured.update(json.loads(Path(source).read_text()))

        self.guest.upload_file = AsyncMock(side_effect=upload)
        result = {"stdout": "任务✓", "stderr": "", "return_code": 7, "timed_out": False}
        self.guest.powershell = AsyncMock(side_effect=["", "null", json.dumps(result)])
        with patch("kubevirt_windows.transport.asyncio.sleep", new_callable=AsyncMock):
            actual = await self.guest.execute(
                "echo %KEY% & exit /b 7", cwd="C:/任务", env={"KEY": "fake-secret"}, timeout=600,
            )
        self.assertEqual(actual, result)
        self.assertEqual(captured["env"], {"KEY": "fake-secret"})
        calls = self.guest.powershell.await_args_list
        self.assertIn("Start-Process", calls[0].args[0])
        self.assertTrue(all("fake-secret" not in call.args[0] for call in calls))
        self.assertTrue(all(call.kwargs["timeout"] < 120 for call in calls))

    async def test_timeout_and_cancellation_signal_supervisor(self):
        self.guest.upload_file = AsyncMock()
        self.guest.powershell = AsyncMock(side_effect=["", '{"timed_out":true}'])
        with self.assertRaises(TimeoutError):
            await self.guest.execute("task", cwd="C:/work", env={}, timeout=600)
        self.guest.powershell = AsyncMock(side_effect=["", asyncio.CancelledError(), ""])
        with self.assertRaises(asyncio.CancelledError):
            await self.guest.execute("task", cwd="C:/work", env={}, timeout=600)
        self.assertIn(".cancel", self.guest.powershell.await_args_list[-1].args[0])

    async def test_download_rejects_bad_chunks_and_preserves_destination(self):
        target = self.root / "result.bin"
        target.write_bytes(b"original")
        for result in (
            {"success": True}, {"success": True, "content_b64": "invalid!"},
            {"success": False, "error": "fake-secret"},
            {"success": True, "content_b64": base64.b64encode(b"12345").decode()},
        ):
            await self.respond(httpx.Response(200, text=sse(result)))
            with patch("kubevirt_windows.ale.CHUNK_BYTES", 4), self.assertRaises(RuntimeError):
                await self.guest.download_file("C:/result.bin", target)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(self.root.iterdir()), [target])

    async def test_download_deadline_covers_all_chunks(self):
        async def slow_chunk(*args, **kwargs):
            await asyncio.sleep(0.015)
            return {"success": True, "content_b64": "YQ=="}

        self.guest.settings.transfer_timeout = 0.025
        self.guest._cmd = AsyncMock(side_effect=slow_chunk)
        with patch("kubevirt_windows.ale.CHUNK_BYTES", 1), self.assertRaises(TimeoutError):
            await self.guest.download_file("C:/result.bin", self.root / "result.bin")
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_upload_rejects_invalid_paths_before_network(self):
        self.guest._cmd = AsyncMock()
        self.guest.powershell = AsyncMock()
        source = self.root / "source.bin"
        source.write_bytes(b"abc")
        link = self.root / "link.bin"
        link.symlink_to(source)
        for path, target in ((link, "C:/out.bin"), (source, "C:/../out.bin")):
            with self.assertRaises(ValueError):
                await self.guest.upload_file(path, target)
        self.guest.powershell.assert_not_awaited()
        self.guest._cmd.assert_not_awaited()

    async def test_upload_failure_cleans_staging_without_publishing_or_retrying(self):
        source = self.root / "source.bin"
        source.write_bytes(b"abc")
        self.guest.powershell = AsyncMock()
        self.guest._cmd = AsyncMock(side_effect=RuntimeError("transport failure"))
        with self.assertRaisesRegex(RuntimeError, "transport failure"):
            await self.guest.upload_file(source, "C:/out.bin")
        self.guest._cmd.assert_awaited_once()
        calls = self.guest.powershell.await_args_list
        self.assertTrue(all("Move-Item" not in call.args[0] for call in calls))
        self.assertIn("Remove-Item", calls[-1].args[0])

    async def test_upload_deadline_covers_all_chunks(self):
        source = self.root / "source.bin"
        source.write_bytes(b"abc")

        async def slow_write(*args, **kwargs):
            await asyncio.sleep(0.015)
            return {"success": True}

        self.guest.settings.transfer_timeout = 0.025
        self.guest.powershell = AsyncMock()
        self.guest._cmd = AsyncMock(side_effect=slow_write)
        with patch("kubevirt_windows.ale.CHUNK_BYTES", 1), self.assertRaises(TimeoutError):
            await self.guest.upload_file(source, "C:/out.bin")
        calls = self.guest.powershell.await_args_list
        self.assertTrue(all("Move-Item" not in call.args[0] for call in calls))
        self.assertIn("Remove-Item", calls[-1].args[0])


class ALELoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_returns_first_event_while_server_keeps_stream_open(self):
        release = threading.Event()
        calls = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                # Native CUA can label SSE as text/plain.
                self.send_header("Content-Type", "text/plain")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                wire = ('\ufeff: heartbeat\r\n\r\nevent: result\r\n'
                        'data: {"success":true,\r\ndata: "return_code":0,"stdout":"5Lu75Yqh4pyT"}\r\n\r\n').encode()
                for byte in wire:
                    self.wfile.write(b"1\r\n" + bytes([byte]) + b"\r\n")
                    self.wfile.flush()
                release.wait(5)
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                guest = ALETransport(SimpleNamespace(waa_port=5000), "trial", "127.0.0.1",
                                     Path(tmp), guest_port=server.server_port)
                try:
                    self.assertEqual(await guest.powershell("echo x", timeout=1), "任务✓")
                    self.assertFalse(release.is_set())
                    self.assertEqual(len(calls), 1)
                finally:
                    await guest.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()

    async def test_real_http_sse_and_chunked_binary_roundtrip(self):
        files = {}
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, wire):
                # Fragment SSE across writes to exercise HTTP stream assembly.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                for start in range(0, len(wire), 7):
                    self.wfile.write(wire[start:start + 7])

            def do_GET(self):
                self.server.test.assertEqual(self.path, "/status")
                self.respond(b'{"status":"ok"}')

            def do_POST(self):
                self.server.test.assertEqual(self.path, "/cmd")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((body, self.headers.get("Authorization")))
                command, params = body["command"], body["params"]
                result = {"success": True}
                if command == "write_bytes":
                    files[params["path"]] = base64.b64decode(params["content_b64"], validate=True)
                elif command == "read_bytes":
                    content = files[params["path"]]
                    chunk = content[params["offset"]:params["offset"] + params["length"]]
                    result["content_b64"] = base64.b64encode(chunk).decode()
                elif command == "run_command":
                    script = base64.b64decode(params["command"].split()[-1]).decode("utf-16-le")
                    # Only model file operations for the HTTP transfer contract;
                    # actual PowerShell/process behavior needs a Windows smoke run.
                    paths = re.findall(r"'((?:[^']|'')*)'", script)
                    paths = [p.replace("''", "'") for p in paths if p.startswith("C:/")]
                    if "WriteAllBytes" in script:
                        files[paths[0]] = b""
                    elif "CopyTo" in script:
                        files[paths[1]] += files[paths[0]]
                    elif "Move-Item" in script:
                        files[paths[1]] = files.pop(paths[0])
                    elif "Remove-Item" in script:
                        for path in paths:
                            files.pop(path, None)
                    result.update(return_code=0, stdout="", stderr="")
                else:
                    raise AssertionError(command)
                self.respond((': heartbeat\r\n' + sse(result)).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.test = self
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                guest = ALETransport(
                    SimpleNamespace(waa_port=5000, transfer_timeout=5),
                    "trial", "127.0.0.1", root, guest_port=server.server_port,
                )
                try:
                    await guest.probe()
                    with patch("kubevirt_windows.ale.CHUNK_BYTES", 1024):
                        for content in (b"", bytes(range(256)) * 8, b"\x00\xffbinary\n" * 300):
                            source = root / "source.bin"
                            source.write_bytes(content)
                            remote = "C:/任务/a'b.bin"
                            await guest.upload_file(source, remote)
                            await guest.download_file(remote, root / "out.bin")
                            self.assertEqual((root / "out.bin").read_bytes(), content)
                            self.assertEqual(files, {remote: content})
                    writes = [body for body, _ in calls if body["command"] == "write_bytes"]
                    self.assertGreater(len(writes), 3)
                    self.assertTrue(all(len(base64.b64decode(b["params"]["content_b64"])) <= 1024 for b in writes))
                    self.assertTrue(all(auth is None for _, auth in calls))
                finally:
                    await guest.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
