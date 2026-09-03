from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MODULE_PATH = Path(__file__).resolve().parents[1] / "dsh_sampling_relay.py"


class Upstream(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(body)
        self.requests.append(
            {"headers": dict(self.headers), "path": self.path, "payload": payload}
        )
        if self.path.endswith("/error"):
            response = b'{"error":"upstream"}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if payload.get("stream"):
            if self.path.endswith("/tool-stream"):
                response = b"".join(
                    (
                        (
                            b'data: {"choices":[{"index":0,"delta":{"tool_calls":'
                            b'[{"index":0,"id":"call-1","function":{"name":"bash",'
                            b'"arguments":""}}]}}]}\n\n'
                        ),
                        (
                            b'data: {"choices":[{"index":0,"delta":{"tool_calls":'
                            b'[{"index":0,"id":"","function":{"name":null,'
                            b'"arguments":"{\\"command\\":\\"pwd\\"}"}}]}}]}\n\n'
                        ),
                        b"data: [DONE]\n\n",
                    )
                )
            else:
                response = b'data: {"ok":true}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        response = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", "req-test")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class SamplingRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True
        )
        cls.upstream_thread.start()
        os.environ["DSH_SAMPLING_UPSTREAM_BASE_URL"] = (
            f"http://127.0.0.1:{cls.upstream.server_port}/v1"
        )
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["DSH_SAMPLING_RECEIPT_PATH"] = str(
            Path(cls.temporary.name) / "receipt.jsonl"
        )
        spec = importlib.util.spec_from_file_location("dsh_sampling_relay", MODULE_PATH)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), cls.module.Relay)
        cls.relay_thread = threading.Thread(target=cls.relay.serve_forever, daemon=True)
        cls.relay_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.relay.shutdown()
        cls.upstream.shutdown()
        cls.relay.server_close()
        cls.upstream.server_close()
        cls.temporary.cleanup()

    def request(self, path: str = "/v1/chat/completions", *, stream: bool = False):
        payload = {
            "model": "deepseek-v4",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "tools": [{"type": "function", "function": {"name": "bash"}}],
            "temperature": 0.2,
            "top_p": 0.4,
            "reasoning_effort": "low",
            "stream": stream,
        }
        return urlopen(
            Request(
                f"http://127.0.0.1:{self.relay.server_port}{path}",
                data=json.dumps(payload).encode(),
                headers={
                    "Accept-Encoding": "gzip",
                    "Authorization": "Bearer fake-key",
                    "Content-Type": "application/json",
                },
            )
        )

    def test_fixes_parameters_and_writes_redacted_receipt(self) -> None:
        with self.request() as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'{"ok":true}')
        forwarded = Upstream.requests[-1]
        payload = forwarded["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 0.95)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(forwarded["path"], "/v1/chat/completions")
        headers = forwarded["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["Authorization"], "Bearer fake-key")

        receipt_path = Path(self.temporary.name) / "receipt.jsonl"
        for _ in range(100):
            if receipt_path.exists() and receipt_path.stat().st_size:
                break
            time.sleep(0.01)
        receipt = json.loads(receipt_path.read_text().splitlines()[-1])
        self.assertEqual(
            receipt["effective"],
            {"reasoning_effort": "max", "temperature": 1.0, "top_p": 0.95},
        )
        receipt_text = json.dumps(receipt)
        self.assertNotIn("secret prompt", receipt_text)
        self.assertNotIn("fake-key", receipt_text)

    def test_concurrent_receipts_remain_valid_json_lines(self) -> None:
        receipt_path = Path(self.temporary.name) / "concurrent-receipt.jsonl"
        original_receipt = self.module.RECEIPT
        self.module.RECEIPT = receipt_path
        try:
            threads = [
                threading.Thread(
                    target=self.module._append_receipt, args=({"index": index},)
                )
                for index in range(32)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            self.module.RECEIPT = original_receipt

        records = [json.loads(line) for line in receipt_path.read_text().splitlines()]
        self.assertEqual(sorted(record["index"] for record in records), list(range(32)))

    def test_stream_preserves_sse_body(self) -> None:
        with self.request(stream=True) as response:
            self.assertEqual(response.read(), b'data: {"ok":true}\n\ndata: [DONE]\n\n')

    def test_stream_restores_empty_tool_call_metadata(self) -> None:
        with self.request("/v1/tool-stream", stream=True) as response:
            lines = [
                line
                for line in response.read().splitlines()
                if line.startswith(b"data: {")
            ]
        chunks = [json.loads(line.removeprefix(b"data: ")) for line in lines]
        calls = [chunk["choices"][0]["delta"]["tool_calls"][0] for chunk in chunks]
        self.assertEqual([call["id"] for call in calls], ["call-1"] * 2)
        self.assertEqual([call["function"]["name"] for call in calls], ["bash"] * 2)

    def test_passes_through_upstream_error(self) -> None:
        with self.assertRaises(HTTPError) as captured:
            self.request("/v1/error")
        self.assertEqual(captured.exception.code, 429)
        self.assertEqual(captured.exception.read(), b'{"error":"upstream"}')


if __name__ == "__main__":
    unittest.main()
