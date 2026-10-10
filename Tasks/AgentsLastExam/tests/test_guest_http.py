"""Check binary HTTP forwarding and SSE servers that keep connections open."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class GuestHTTPTests(unittest.TestCase):
    def forward(self, content_type, body, streaming=False):
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_):
                pass
            def do_POST(self):
                received = self.rfile.read(int(self.headers["Content-Length"]))
                if received != b"binary request\x00\xff":
                    self.send_error(400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if streaming:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if streaming:
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                    self.wfile.flush()
                    release.wait(5)
                    self.close_connection = True
                else:
                    self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "request.json").write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/cmd",
                    "method": "POST", "headers": {"Content-Type": "application/octet-stream"},
                    "body": base64.b64encode(b"binary request\x00\xff").decode(), "timeout": 2}))
                result = subprocess.run([sys.executable, "-m", "ale_adapter.guest_http", str(root / "request.json"),
                    str(root / "response")], capture_output=True, timeout=4, check=False,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual((root / "response.body").read_bytes(), body)
                metadata = json.loads((root / "response.json").read_text())
                self.assertEqual(metadata, {"status": 200, "content_type": content_type})
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_binary_payload_is_unchanged(self):
        self.forward("application/octet-stream", b"\x00\xff\x80payload\n")

    def test_sse_returns_after_first_event_without_waiting_for_eof(self):
        self.forward("text/event-stream", b'data: {"success": true}\n\n', streaming=True)
