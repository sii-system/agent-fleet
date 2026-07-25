from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import pi_pr_review as pi_review


# -- stub pi binary helpers ------------------------------------------------


def _stub_pi_script(
    bin_dir: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    """Write a stub ``pi`` script that echoes captured args and returns
    controlled output."""
    pi = bin_dir / "pi"
    pi.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
stub_dir="$(cd "$(dirname "$0")" && pwd)"
prompt="${{@: -1}}"
{{
  printf 'home=%s\\n' "${{HOME:-}}"
  printf 'pi_dir=%s\\n' "${{PI_CODING_AGENT_DIR:-}}"
  printf 'cwd=%s\\n' "$PWD"
  printf 'allowed_paths=%s\\n' "${{HARBOR_ANALYZER_ALLOWED_PATHS_JSON:-}}"
  printf 'grep_literal_only=%s\\n' "${{HARBOR_ANALYZER_GREP_LITERAL_ONLY:-}}"
  printf 'max_tool_calls=%s\\n' "${{HARBOR_ANALYZER_MAX_TOOL_CALLS:-}}"
  printf 'offline=%s\\n' "${{PI_OFFLINE:-}}"
  printf 'token=%s\\n' "${{AGENT_FLEET_API_KEY:-}}"
  printf 'prompt=<%s>\\n' "$prompt"
  printf 'arg=<%s>\\n' "$@"
  printf 'models=\\n'
  if [[ -n "${{PI_CODING_AGENT_DIR:-}}" ]]; then
    cat "${{PI_CODING_AGENT_DIR}}/models.json" 2>/dev/null || true
  fi
}} >>"$stub_dir/pi-capture.txt"
cat >&2 <<'ERR'
{stderr}
ERR
cat <<'OUT'
{stdout}
OUT
exit {exit_code}
""",
        encoding="utf-8",
    )
    pi.chmod(0o755)
    return pi


def _make_findings_response(
    findings: list[dict] | None = None,
    *,
    stop_reason: str = "stop",
) -> str:
    """Build a valid pi JSONL response with a findings payload."""
    if findings is None:
        findings = [
            {
                "severity": "P1",
                "path": "src/worker.py",
                "line": 2,
                "title": "Cancellation not forwarded",
                "failure_scenario": "Worker survives wrapper.",
                "remediation": "Terminate the process group.",
            }
        ]
    text = json.dumps({"findings": findings}, separators=(",", ":"))
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": stop_reason,
    }
    events = [
        {"type": "session", "id": "session-1"},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": assistant},
        {"type": "turn_end", "message": assistant},
        {"type": "agent_end"},
    ]
    return "\n".join(json.dumps(e) for e in events)


def _make_tool_execution_stream(count: int) -> str:
    events = [
        json.loads(line)
        for line in _make_findings_response([]).splitlines()
    ]
    insertion = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "turn_end"
    )
    events[insertion:insertion] = [
        {
            "type": "tool_execution_start",
            "toolCallId": f"call-{index}",
            "toolName": "read",
            "args": {"path": "evidence.txt"},
        }
        for index in range(count)
    ]
    return "\n".join(json.dumps(event) for event in events)


def _require_pi_081(test_case: unittest.TestCase) -> str:
    pi_binary = shutil.which("pi")
    if pi_binary is None:
        test_case.skipTest(
            "pi runtime integration requires pi 0.81.1; "
            "pi binary was not found"
        )
    version_result = subprocess.run(
        [pi_binary, "--version"],
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    version = version_result.stdout.strip()
    if version_result.returncode != 0 or version != "0.81.1":
        test_case.skipTest(
            "pi runtime integration requires pi 0.81.1; "
            f"pi --version reported {version or 'no version'}"
        )
    return pi_binary


def _sse_body(chunks: list[dict]) -> bytes:
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _tool_call(
    index: int,
    tool_call_id: str,
    name: str,
    arguments: dict[str, object],
) -> dict:
    return {
        "index": index,
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _tool_response_chunks(
    model: str,
    response_id: str,
    tool_calls: list[dict],
) -> list[dict]:
    return [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ],
        },
    ]


def _final_response_chunks(model: str, response_id: str) -> list[dict]:
    return [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": '{"findings":[]}',
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    ]


@contextmanager
def _serve_sse(response_bodies: list[bytes]):
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            request_index = len(requests)
            requests.append(
                {
                    "path": self.path,
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            response_body = response_bodies[request_index]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    server_thread.start()
    try:
        yield server.server_address[1], requests
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        if server_thread.is_alive():
            raise RuntimeError("fake SSE server did not stop")


def _run_real_pi_exchange(
    test_case: unittest.TestCase,
    *,
    endpoint: str,
    tool_calls: list[dict],
    files: dict[str, str],
    unset_reviewer_policy: bool = False,
    expect_error: str | None = None,
    bypass_stream_validation: bool = False,
) -> tuple[dict | None, list[dict], list[dict]]:
    pi_binary = _require_pi_081(test_case)
    model = "integration-model"
    response_bodies = [
        _sse_body(
            _tool_response_chunks(
                model,
                "chatcmpl-integration",
                tool_calls,
            )
        ),
        _sse_body(
            _final_response_chunks(
                model,
                "chatcmpl-integration-final",
            )
        ),
    ]
    with (
        _serve_sse(response_bodies) as (port, requests),
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        repository_root = Path(temp_dir) / "repository"
        repository_root.mkdir()
        for relative_path, content in files.items():
            (repository_root / relative_path).write_text(
                content,
                encoding="utf-8",
            )
        launch_binary = pi_binary
        if unset_reviewer_policy:
            wrapper = Path(temp_dir) / "pi-without-reviewer-policy"
            wrapper.write_text(
                (
                    "#!/usr/bin/env python3\n"
                    "import os\n"
                    "import sys\n"
                    f"os.environ.pop({pi_review.PI_GREP_LITERAL_ONLY_ENV!r}, None)\n"
                    f"os.environ.pop({pi_review.PI_MAX_TOOL_CALLS_ENV!r}, None)\n"
                    f"pi_binary = {pi_binary!r}\n"
                    "os.execv(pi_binary, [pi_binary, *sys.argv[1:]])\n"
                ),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            launch_binary = str(wrapper)
        client = pi_review.PiClient(
            pi_binary=launch_binary,
            base_url=f"http://127.0.0.1:{port}{endpoint}",
            api_key="integration-api-key",
            model=model,
            repository_root=repository_root,
            timeout=30,
        )
        pi_streams: list[str] = []
        validate_stream = pi_review._validate_pi_stream

        def capture_stream(raw: str) -> dict:
            pi_streams.append(raw)
            if bypass_stream_validation:
                return {"findings": []}
            return validate_stream(raw)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                },
            ),
            mock.patch.object(
                pi_review,
                "_validate_pi_stream",
                side_effect=capture_stream,
            ),
        ):
            if expect_error is None:
                result = client.review(
                    "Use the requested tools, then return an empty findings array.",
                    "Inspect the repository as requested.",
                )
            else:
                with test_case.assertRaisesRegex(
                    pi_review.PiReviewError,
                    expect_error,
                ):
                    client.review(
                        "Use the requested tools, then return an empty findings array.",
                        "Inspect the repository as requested.",
                    )
                result = None

    events = (
        [
            json.loads(line)
            for line in pi_streams[0].splitlines()
            if line.strip()
        ]
        if pi_streams
        else []
    )
    return result, requests, events


# -- URL normalisation ----------------------------------------------------


class UrlNormalisationTest(unittest.TestCase):
    def test_strips_chat_completions_suffix(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://api.example.com/v1/chat/completions"
        )
        self.assertEqual(result, "https://api.example.com/v1")

    def test_strips_chat_completions_suffix_with_trailing_slash(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://api.example.com/v1/chat/completions/"
        )
        self.assertEqual(result, "https://api.example.com/v1")

    def test_preserves_custom_prefix(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://gateway.example.com/v3/chat/completions"
        )
        self.assertEqual(result, "https://gateway.example.com/v3")

    def test_preserves_already_clean_url(self) -> None:
        result = pi_review._chat_url_to_base("https://api.example.com/v1")
        self.assertEqual(result, "https://api.example.com/v1")

    def test_preserves_clean_url_trailing_slash(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://api.example.com/custom/"
        )
        self.assertEqual(result, "https://api.example.com/custom/")

    def test_rejects_invalid_url(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._chat_url_to_base("not-a-url")

    def test_rejects_malformed_url(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._chat_url_to_base("http://[::1")

    def test_rejects_query_parameters(self) -> None:
        with self.assertRaisesRegex(
            pi_review.PiReviewError,
            "query parameters or fragments are not supported",
        ):
            pi_review._chat_url_to_base(
                "https://api.example.com/v1/chat/completions?api-version=1"
            )

    def test_rejects_fragment(self) -> None:
        with self.assertRaisesRegex(
            pi_review.PiReviewError,
            "query parameters or fragments are not supported",
        ):
            pi_review._chat_url_to_base(
                "https://api.example.com/v1/chat/completions#endpoint"
            )


# -- JSON extraction ------------------------------------------------------


class JsonExtractionTest(unittest.TestCase):
    def test_bare_object(self) -> None:
        self.assertEqual(
            pi_review._extract_json('{"findings": []}'),
            {"findings": []},
        )

    def test_fenced_object(self) -> None:
        self.assertEqual(
            pi_review._extract_json('```json\n{"findings": []}\n```'),
            {"findings": []},
        )

    def test_bare_fence(self) -> None:
        self.assertEqual(
            pi_review._extract_json('```\n{"findings": []}\n```'),
            {"findings": []},
        )

    def test_compact_fenced_object(self) -> None:
        self.assertEqual(
            pi_review._extract_json('```json{"findings": []}```'),
            {"findings": []},
        )

    def test_rejects_trailing_text(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._extract_json('{"findings": []} extra')

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._extract_json('"just a string"')

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._extract_json("not json")


# -- JSONL stream validation ----------------------------------------------


class StreamValidationTest(unittest.TestCase):
    def test_empty_stdout_raises(self) -> None:
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream("")
        self.assertIn("no output", str(ctx.exception))

    def test_whitespace_stdout_raises(self) -> None:
        with self.assertRaises(pi_review.PiReviewError):
            pi_review._validate_pi_stream("   \n\t ")

    def test_valid_stream_returns_parsed_findings(self) -> None:
        result = pi_review._validate_pi_stream(
            _make_findings_response()
        )
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "P1")

    def test_empty_findings_are_valid(self) -> None:
        result = pi_review._validate_pi_stream(
            _make_findings_response([])
        )
        self.assertEqual(result["findings"], [])

    def test_exactly_maximum_tool_executions_are_valid(self) -> None:
        result = pi_review._validate_pi_stream(
            _make_tool_execution_stream(16)
        )

        self.assertEqual(result, {"findings": []})

    def test_over_maximum_tool_executions_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            pi_review.PiReviewError,
            "tool-call limit of 16.*observed 17",
        ):
            pi_review._validate_pi_stream(_make_tool_execution_stream(17))

    def test_missing_session_raises(self) -> None:
        events = [
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "turn_end"},
            {"type": "agent_end"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("session lifecycle", str(ctx.exception))

    def test_incomplete_agent_lifecycle_raises(self) -> None:
        events = [
            {"type": "session", "id": "s1"},
            {"type": "agent_start"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("agent lifecycle", str(ctx.exception))

    def test_provider_error_raises(self) -> None:
        response = _make_findings_response()
        events = [json.loads(line) for line in response.splitlines()]
        events.insert(
            -1, {"type": "auto_retry_end", "finalError": "gateway timeout"}
        )
        raw = "\n".join(json.dumps(e) for e in events)
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("gateway timeout", str(ctx.exception))

    def test_stop_reason_aborted_raises(self) -> None:
        raw = _make_findings_response(stop_reason="aborted")
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("aborted", str(ctx.exception))

    def test_missing_stop_reason_raises(self) -> None:
        events = [
            {"type": "session", "id": "s1"},
            {"type": "agent_start"},
            {"type": "turn_start"},
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "{}"}],
                },
            },
            {"type": "agent_end"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("stop reason", str(ctx.exception))

    def test_empty_assistant_text_returns_incomplete(self) -> None:
        assistant = {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "stopReason": "stop",
        }
        events = [
            {"type": "session", "id": "s1"},
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "message_end", "message": assistant},
            {"type": "turn_end", "message": assistant},
            {"type": "agent_end"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = pi_review._validate_pi_stream(raw)
        self.assertEqual(result, {"findings": [], "incomplete": True})

    def test_no_final_assistant_message_raises(self) -> None:
        events = [
            {"type": "session", "id": "s1"},
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "turn_end"},
            {"type": "agent_end"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review._validate_pi_stream(raw)
        self.assertIn("no final assistant message", str(ctx.exception))


# -- PiClient subprocess tests --------------------------------------------


class PiClientTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.repo_dir = self.root / "repository"
        self.repo_dir.mkdir()
        self.capture = self.bin_dir / "pi-capture.txt"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_client(self, **overrides) -> pi_review.PiClient:
        kwargs = dict(
            pi_binary=str(self.bin_dir / "pi"),
            base_url="https://api.example.com/v1/chat/completions",
            api_key="test-api-key",
            model="test-model",
            repository_root=self.repo_dir,
            timeout=30,
        )
        kwargs.update(overrides)
        return pi_review.PiClient(**kwargs)

    def test_runs_from_repository_root(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn(f"cwd={self.repo_dir.resolve()}", captured)

    def test_uses_only_path_gated_read_tools(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("arg=<--no-builtin-tools>", captured)
        self.assertIn("arg=<--tools>", captured)
        self.assertIn("arg=<read,grep,find,ls>", captured)
        self.assertIn("arg=<--extension>", captured)
        self.assertIn(
            f"arg=<{pi_review.PI_PATH_GATE_EXTENSION.resolve()}>",
            captured,
        )
        self.assertIn("arg=<--no-approve>", captured)
        self.assertNotIn("arg=<--approve>", captured)

    def test_limits_path_gate_to_repository_root(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        expected = json.dumps([str(self.repo_dir.resolve())])
        self.assertIn(f"allowed_paths={expected}", captured)

    def test_forces_path_gate_grep_to_literal_matching(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("grep_literal_only=1", captured)

    def test_limits_reviewer_to_sixteen_tool_calls_per_chunk(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertEqual(pi_review.PI_MAX_TOOL_CALLS, 16)
        self.assertIn("max_tool_calls=16", captured)

    def test_path_gate_uses_non_regex_wildcard_matching(self) -> None:
        source = pi_review.PI_PATH_GATE_EXTENSION.read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'import { compileSimpleGlob } from "./simple_glob_matcher.mjs"',
            source,
        )
        self.assertNotIn("function wildcardMatches(", source)
        self.assertNotIn("simpleGlobToRegExp", source)

    def test_missing_repository_root_fails_before_launch(self) -> None:
        missing_root = self.root / "missing-repository"

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            self._make_client(repository_root=missing_root)

        self.assertIn("repository root", str(ctx.exception))

    def test_missing_path_gate_fails_before_launch(self) -> None:
        missing_gate = self.root / "missing-path-gate.ts"

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            self._make_client(path_gate_extension=missing_gate)

        self.assertIn("path-gate extension", str(ctx.exception))

    def test_passes_system_prompt_and_diff_chunk_to_pi(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("You are a reviewer.", "FILE worker.py\n+stop()")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("prompt=<FILE worker.py\n+stop()>", captured)
        self.assertIn("arg=<--system-prompt>", captured)
        self.assertIn("arg=<You are a reviewer.>", captured)
        self.assertIn("arg=<--tools>", captured)
        self.assertIn("arg=<read,grep,find,ls>", captured)
        self.assertIn("arg=<--no-approve>", captured)
        self.assertIn("arg=<--no-session>", captured)
        self.assertIn("offline=1", captured)

    def test_mode_json_and_print_are_set(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("arg=<--mode>", captured)
        self.assertIn("arg=<json>", captured)
        self.assertIn("arg=<--print>", captured)

    def test_models_json_uses_normalised_base_url(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client(
            base_url="https://api.example.com/v1/chat/completions"
        )

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn('"baseUrl": "https://api.example.com/v1"', captured)
        self.assertIn('"api": "openai-completions"', captured)
        self.assertIn('"id": "test-model"', captured)

    def test_api_key_is_passed_via_environment(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client(api_key="secret-key")

        client.review("prompt", "diff")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("token=secret-key", captured)

    def test_non_zero_exit_raises(self) -> None:
        _stub_pi_script(
            self.bin_dir,
            stdout="",
            stderr="pi: fatal error",
            exit_code=1,
        )
        client = self._make_client()

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            client.review("prompt", "diff")
        self.assertIn("exited with code 1", str(ctx.exception))
        self.assertIn("fatal error", str(ctx.exception))

    def test_timeout_raises(self) -> None:
        client = self._make_client(timeout=0, pi_binary="/usr/bin/sleep")
        # sleep 999 should exceed timeout=0
        with mock.patch("subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(
                ["sleep", "999"], 0.001
            )
            with self.assertRaises(pi_review.PiReviewError) as ctx:
                client.review("prompt", "diff")
            self.assertIn("timed out", str(ctx.exception))

    def test_review_uses_effective_timeout_for_subprocess(self) -> None:
        client = self._make_client(timeout=30)
        completed = subprocess.CompletedProcess(
            args=["pi"],
            returncode=0,
            stdout=_make_findings_response(),
            stderr="",
        )

        with mock.patch(
            "subprocess.run", return_value=completed
        ) as run_mock:
            client.review("prompt", "diff", timeout=12.5)

        self.assertEqual(run_mock.call_args.kwargs["timeout"], 12.5)

    def test_timeout_diagnostic_reports_effective_timeout(self) -> None:
        client = self._make_client(timeout=30)
        with mock.patch("subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(
                ["pi"], 12.5
            )
            with self.assertRaises(pi_review.PiReviewError) as ctx:
                client.review("prompt", "diff", timeout=12.5)

        self.assertEqual(str(ctx.exception), "pi timed out after 12.5s")

    def test_pi_not_found_raises(self) -> None:
        client = self._make_client(pi_binary="/nonexistent/pi-binary")

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            client.review("prompt", "diff")
        self.assertIn("could not launch pi", str(ctx.exception))

    def test_invalid_jsonl_raises(self) -> None:
        _stub_pi_script(self.bin_dir, stdout="not-jsonl\n")
        client = self._make_client()

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            client.review("prompt", "diff")
        self.assertIn("invalid jsonl", str(ctx.exception).lower())


class PiRuntimeIntegrationTest(unittest.TestCase):
    def test_real_pi_uses_path_gated_tools_at_adapted_endpoint(self) -> None:
        model = "integration-model"
        result, requests, stream_events = _run_real_pi_exchange(
            self,
            endpoint="/custom/chat/completions",
            tool_calls=[
                _tool_call(
                    0,
                    "call-inside",
                    "read",
                    {"path": "inside.txt"},
                ),
                _tool_call(
                    1,
                    "call-outside",
                    "read",
                    {"path": "/etc/hosts"},
                ),
                _tool_call(
                    2,
                    "call-grep",
                    "grep",
                    {
                        "pattern": "needle",
                        "path": "large.txt",
                        "literal": True,
                        "context": 1_000_000,
                        "limit": 1_000_000,
                    },
                ),
                _tool_call(
                    3,
                    "call-literal-policy",
                    "grep",
                    {
                        "pattern": "-+$",
                        "path": "separator.txt",
                        "literal": False,
                    },
                ),
                _tool_call(
                    4,
                    "call-wildcard",
                    "grep",
                    {
                        "pattern": "wildcard marker",
                        "path": ".",
                        "glob": "review-*separator?.txt",
                        "literal": False,
                    },
                ),
                _tool_call(
                    5,
                    "call-pattern-too-long",
                    "grep",
                    {
                        "pattern": "p" * 1025,
                        "path": ".",
                        "literal": True,
                    },
                ),
                _tool_call(
                    6,
                    "call-glob-too-long",
                    "grep",
                    {
                        "pattern": "marker",
                        "path": ".",
                        "glob": "g" * 257,
                        "literal": True,
                    },
                ),
                _tool_call(
                    7,
                    "call-find-pattern-too-long",
                    "find",
                    {
                        "pattern": "f" * 257,
                        "path": ".",
                    },
                ),
            ],
            files={
                "inside.txt": "inside repository evidence\n",
                "large.txt": "\n".join(
                    f"needle-{index}-{'界' * 400}"
                    for index in range(80)
                ),
                "separator.txt": "----------------\n",
                "review-separator1.txt": "wildcard marker\n",
            },
        )

        self.assertEqual(result, {"findings": []})
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/custom/chat/completions",
                "/custom/chat/completions",
            ],
        )
        self.assertEqual(requests[0]["body"]["model"], model)
        self.assertTrue(requests[0]["body"]["stream"])
        grep_tool = next(
            tool["function"]
            for tool in requests[0]["body"]["tools"]
            if tool["function"]["name"] == "grep"
        )
        self.assertIn(
            "policy may force literal string matching",
            grep_tool["description"],
        )
        tool_results = {
            message["tool_call_id"]: message["content"]
            for message in requests[1]["body"]["messages"]
            if message["role"] == "tool"
        }
        self.assertIn(
            "inside repository evidence",
            tool_results["call-inside"],
        )
        self.assertIn("Access denied", tool_results["call-outside"])
        self.assertEqual(
            tool_results["call-literal-policy"],
            "No matches",
        )
        self.assertIn(
            "wildcard marker",
            tool_results["call-wildcard"],
        )
        self.assertIn(
            "grep pattern exceeds 1024 characters",
            tool_results["call-pattern-too-long"],
        )
        self.assertIn(
            "grep glob exceeds 256 characters",
            tool_results["call-glob-too-long"],
        )
        self.assertIn(
            "find pattern exceeds 256 characters",
            tool_results["call-find-pattern-too-long"],
        )
        grep_result = tool_results["call-grep"]
        self.assertLessEqual(len(grep_result.encode("utf-8")), 50 * 1024)
        self.assertIn(
            "[Output truncated by analyzer path gate:",
            grep_result,
        )
        self.assertIn("200 matches", grep_result)
        self.assertIn("20 context lines", grep_result)
        self.assertIn("50KB UTF-8 output", grep_result)
        grep_event = next(
            event
            for event in stream_events
            if event["type"] == "tool_execution_end"
            and event["toolCallId"] == "call-grep"
        )
        literal_event = next(
            event
            for event in stream_events
            if event["type"] == "tool_execution_end"
            and event["toolCallId"] == "call-literal-policy"
        )
        self.assertTrue(
            literal_event["result"]["details"]["effective_literal"]
        )
        self.assertTrue(
            literal_event["result"]["details"]["literal_forced"]
        )
        self.assertEqual(
            grep_event["result"]["details"]["effective_limit"],
            200,
        )
        self.assertEqual(
            grep_event["result"]["details"]["effective_context"],
            20,
        )
        self.assertTrue(grep_event["result"]["details"]["truncated"])
        self.assertLessEqual(
            grep_event["result"]["details"]["output_bytes"],
            50 * 1024,
        )

    def test_real_pi_aborts_over_budget_tool_batch_before_follow_up(
        self,
    ) -> None:
        result, requests, events = _run_real_pi_exchange(
            self,
            endpoint="/budget/chat/completions",
            tool_calls=[
                _tool_call(
                    index,
                    f"call-{index}",
                    "read",
                    {"path": f"evidence-{index}.txt"},
                )
                for index in range(17)
            ],
            files={
                f"evidence-{index}.txt": f"evidence {index}\n"
                for index in range(17)
            },
            expect_error="tool-call limit of 16.*observed 17",
        )

        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)
        completed = [
            event
            for event in events
            if event["type"] == "tool_execution_end"
        ]
        self.assertEqual(
            sum(not event["isError"] for event in completed),
            16,
        )
        over_limit = next(
            event
            for event in completed
            if event["toolCallId"] == "call-16"
        )
        self.assertTrue(over_limit["isError"])

    def test_real_pi_caps_read_find_and_ls_outputs(self) -> None:
        long_files = {
            f"{index:03d}-{'x' * 180}.txt": "evidence\n"
            for index in range(220)
        }
        result, requests, events = _run_real_pi_exchange(
            self,
            endpoint="/caps/chat/completions",
            tool_calls=[
                _tool_call(
                    0,
                    "call-read-cap",
                    "read",
                    {"path": "large.txt", "limit": 1_000_000},
                ),
                _tool_call(
                    1,
                    "call-find-cap",
                    "find",
                    {"pattern": "*", "path": ".", "limit": 1_000_000},
                ),
                _tool_call(
                    2,
                    "call-ls-cap",
                    "ls",
                    {"path": ".", "limit": 1_000_000},
                ),
                _tool_call(
                    3,
                    "call-denied-cap",
                    "read",
                    {"path": "z" * 60_000},
                ),
            ],
            files={
                "large.txt": (
                    "API_KEY=sk-abcdefghijklmnop\n"
                    + "界" * 30_000
                    + "\n"
                ),
                **long_files,
            },
        )

        self.assertEqual(result, {"findings": []})
        tool_results = {
            message["tool_call_id"]: message["content"]
            for message in requests[1]["body"]["messages"]
            if message["role"] == "tool"
        }
        for tool_call_id in (
            "call-read-cap",
            "call-find-cap",
            "call-ls-cap",
            "call-denied-cap",
        ):
            output = tool_results[tool_call_id]
            self.assertLessEqual(len(output.encode("utf-8")), 50 * 1024)
            self.assertIn(
                "[Output truncated by analyzer path gate:",
                output,
            )
            self.assertNotIn("\ufffd", output)
        self.assertIn("<REDACTED>", tool_results["call-read-cap"])
        self.assertNotIn(
            "sk-abcdefghijklmnop",
            tool_results["call-read-cap"],
        )

        details = {
            event["toolCallId"]: event["result"]["details"]
            for event in events
            if event["type"] == "tool_execution_end"
        }
        for tool_call_id in (
            "call-read-cap",
            "call-find-cap",
            "call-ls-cap",
            "call-denied-cap",
        ):
            self.assertTrue(details[tool_call_id]["truncated"])
            self.assertLessEqual(
                details[tool_call_id]["output_bytes"],
                details[tool_call_id]["output_byte_limit"],
            )
        self.assertEqual(
            details["call-read-cap"]["effective_limit"],
            1200,
        )
        self.assertEqual(
            details["call-find-cap"]["effective_limit"],
            200,
        )
        self.assertEqual(
            details["call-ls-cap"]["effective_limit"],
            200,
        )

    def test_real_pi_has_no_tool_count_limit_without_reviewer_env(
        self,
    ) -> None:
        result, requests, events = _run_real_pi_exchange(
            self,
            endpoint="/harbor-unlimited/chat/completions",
            tool_calls=[
                _tool_call(
                    index,
                    f"call-{index}",
                    "read",
                    {"path": "evidence.txt"},
                )
                for index in range(17)
            ],
            files={"evidence.txt": "shared Harbor evidence\n"},
            unset_reviewer_policy=True,
            bypass_stream_validation=True,
        )

        self.assertEqual(result, {"findings": []})
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            sum(
                event["type"] == "tool_execution_end"
                and not event["isError"]
                for event in events
            ),
            17,
        )

    def test_real_pi_preserves_regex_grep_without_reviewer_policy(
        self,
    ) -> None:
        result, requests, events = _run_real_pi_exchange(
            self,
            endpoint="/harbor/chat/completions",
            tool_calls=[
                _tool_call(
                    0,
                    "call-regex",
                    "grep",
                    {
                        "pattern": "^section-[0-9]+$",
                        "path": "evidence.txt",
                        "literal": False,
                    },
                )
            ],
            files={"evidence.txt": "section-42\n"},
            unset_reviewer_policy=True,
        )

        self.assertEqual(result, {"findings": []})
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/harbor/chat/completions",
                "/harbor/chat/completions",
            ],
        )
        tool_result = next(
            message["content"]
            for message in requests[1]["body"]["messages"]
            if message.get("tool_call_id") == "call-regex"
        )
        self.assertIn("section-42", tool_result)
        grep_event = next(
            event
            for event in events
            if event["type"] == "tool_execution_end"
            and event["toolCallId"] == "call-regex"
        )
        self.assertFalse(
            grep_event["result"]["details"]["effective_literal"]
        )
        self.assertFalse(
            grep_event["result"]["details"]["literal_forced"]
        )


# -- orchestration tests --------------------------------------------------


class FakeGitHub:
    def __init__(self) -> None:
        self.pull = {
            "draft": False,
            "head": {"sha": "head-1"},
            "title": "Change worker cancellation",
            "body": "Keep child processes from leaking.",
        }
        self.files = [
            {
                "filename": "worker.py",
                "patch": "@@ -1 +1,2 @@\n keep\n+stop()",
            }
        ]
        self.reviews: list[dict] = []
        self.created: list[tuple] = []

    def get_pull(self, _number: int) -> dict:
        return self.pull

    def list_files(self, _number: int) -> list[dict]:
        return self.files

    def list_reviews(self, _number: int) -> list[dict]:
        return self.reviews

    def create_review(self, *args: object) -> dict[str, int]:
        self.created.append(args)
        return {"id": 1}


class FakePiClient:
    def __init__(
        self,
        findings: list[dict] | None = None,
        *,
        timeout: float = pi_review.PI_TIMEOUT_SECONDS,
    ) -> None:
        if findings is None:
            findings = [
                {
                    "severity": "P1",
                    "path": "worker.py",
                    "line": 2,
                    "title": "Cancellation not forwarded",
                    "failure_scenario": "Worker survives wrapper.",
                    "remediation": "Terminate the process group.",
                }
            ]
        self.findings = findings
        self.inputs: list[str] = []
        self.timeout = timeout
        self.timeouts: list[float] = []

    def review(
        self,
        _prompt: str,
        chunk: str,
        *,
        timeout: float,
    ) -> dict:
        self.inputs.append(chunk)
        self.timeouts.append(timeout)
        return {"findings": list(self.findings)}


class OrchestrationTest(unittest.TestCase):
    def test_publishes_review_with_findings(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient()

        result = pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertEqual(result, "published")
        number, sha, body, findings = github.created[0]
        self.assertEqual((number, sha), (7, "head-1"))
        self.assertIn("<!-- pi-pr-review:head-1 -->", body)
        self.assertEqual(len(findings), 1)

    def test_no_findings_still_posts_summary(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])

        pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertIn("no actionable findings", github.created[0][2])

    def test_duplicate_review_is_skipped(self) -> None:
        github = FakeGitHub()
        github.reviews = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- pi-pr-review:head-1 -->",
            }
        ]

        result = pi_review.run_review(
            github, FakePiClient(), 7, "prompt"
        )

        self.assertEqual(result, "duplicate")
        self.assertEqual(github.created, [])

    def test_stale_head_is_not_published(self) -> None:
        github = FakeGitHub()
        first = dict(github.pull)
        second = {
            **github.pull,
            "head": {**github.pull["head"], "sha": "head-2"},
        }
        github.get_pull = mock.Mock(side_effect=[first, second])

        result = pi_review.run_review(
            github, FakePiClient(), 7, "prompt"
        )

        self.assertEqual(result, "stale")
        self.assertEqual(github.created, [])

    def test_pr_context_is_passed_to_pi(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient()

        pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertIn("PR TITLE:", pi_client.inputs[0])
        self.assertIn("Change worker cancellation", pi_client.inputs[0])
        self.assertIn("UNTRUSTED DIFF", pi_client.inputs[0])

    def test_incomplete_chunk_is_reported(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()
        pi_client.timeout = pi_review.PI_TIMEOUT_SECONDS
        pi_client.review.return_value = {
            "findings": [],
            "incomplete": True,
        }

        pi_review.run_review(github, pi_client, 7, "prompt")

        body = github.created[0][2]
        self.assertIn("Coverage: Partial", body)
        self.assertIn("empty model response", body)

    def test_custom_review_id_is_used(self) -> None:
        github = FakeGitHub()

        pi_review.run_review(
            github,
            FakePiClient([]),
            7,
            "prompt",
            review_id="custom-review-id",
        )

        body = github.created[0][2]
        self.assertIn("<!-- custom-review-id:head-1 -->", body)

    def test_four_chunks_share_one_recalculated_review_budget(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])
        chunks = ["chunk-1", "chunk-2", "chunk-3", "chunk-4"]

        with (
            mock.patch.object(
                pi_review._review,
                "build_chunks",
                return_value=(chunks, False),
            ),
            mock.patch.object(
                pi_review.time,
                "monotonic",
                side_effect=[100.0, 100.0, 200.0, 400.0, 700.0],
            ),
        ):
            result = pi_review.run_review(
                github, pi_client, 7, "prompt"
            )

        self.assertEqual(result, "published")
        expected = [225.0, 800.0 / 3, 300.0, 300.0]
        self.assertEqual(len(pi_client.timeouts), 4)
        for actual, wanted in zip(
            pi_client.timeouts, expected, strict=True
        ):
            self.assertAlmostEqual(actual, wanted)
        self.assertTrue(
            all(
                timeout < pi_review.PI_TIMEOUT_SECONDS
                for timeout in pi_client.timeouts
            )
        )

    def test_chunk_timeout_is_capped_by_client_maximum(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([], timeout=30)

        with mock.patch.object(
            pi_review.time,
            "monotonic",
            side_effect=[0.0, 0.0],
        ):
            pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertEqual(pi_client.timeouts, [30])

    def test_exhausted_review_budget_fails_before_launch(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])

        with (
            mock.patch.object(
                pi_review.time,
                "monotonic",
                side_effect=[
                    0.0,
                    float(pi_review.PI_REVIEW_BUDGET_SECONDS),
                ],
            ),
            self.assertRaises(pi_review.PiReviewError) as ctx,
        ):
            pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertEqual(
            str(ctx.exception),
            "pi review deadline exhausted before chunk 1 of 1",
        )
        self.assertEqual(pi_client.inputs, [])


# -- main entrypoint tests -------------------------------------------------


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.event_path = self.root / "event.json"
        self.event_path.write_text(
            json.dumps({"pull_request": {"number": 23}}),
            encoding="utf-8",
        )
        self.prompt_path = self.root / "prompt.md"
        self.prompt_path.write_text(
            "Review this pull request.", encoding="utf-8"
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.environment = {
            "GITHUB_REPOSITORY": "sii-system/agent-fleet",
            "GITHUB_TOKEN": "fake-github-token",
            "GITHUB_WORKSPACE": str(self.workspace),
            "LLM_REVIEW_BASE_URL": (
                "https://api.example.com/v1/chat/completions"
            ),
            "LLM_REVIEW_API_KEY": "fake-review-api-key",
            "LLM_REVIEW_MODEL": "fake-review-model",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def argv(self) -> list[str]:
        return [
            "pi_pr_review.py",
            "--event-path",
            str(self.event_path),
            "--prompt-path",
            str(self.prompt_path),
        ]

    def test_main_passes_github_workspace_to_pi_client(self) -> None:
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(sys, "argv", self.argv()),
            mock.patch.object(pi_review, "PiClient") as pi_client,
            mock.patch.object(
                pi_review, "run_review", return_value="published"
            ),
        ):
            result = pi_review.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            pi_client.call_args.kwargs["repository_root"],
            self.workspace,
        )

    def test_main_reports_repository_validation_error(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(sys, "argv", self.argv()),
            mock.patch.object(
                pi_review,
                "PiClient",
                side_effect=pi_review.PiReviewError("bad repository root"),
            ),
            redirect_stderr(stderr),
        ):
            result = pi_review.main()

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue(),
            "pi PR review failed: bad repository root\n",
        )


# -- workflow contract tests -----------------------------------------------


class PiWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT_DIR / "pi_pr_review.py"

    def test_script_uses_pi_review_id(self) -> None:
        self.assertEqual(pi_review.PI_REVIEW_ID, "pi-pr-review")

    def test_script_imports_shared_components(self) -> None:
        self.assertIsNotNone(pi_review._review.GitHubClient)
        self.assertIsNotNone(pi_review._review.validate_findings)
        self.assertIsNotNone(pi_review._review.build_chunks)

    def test_pi_timeout_is_longer_than_raw_api(self) -> None:
        self.assertGreater(
            pi_review.PI_TIMEOUT_SECONDS, 600,
            "agent review should allow more time than direct API calls",
        )

    def test_review_budget_reserves_time_within_workflow_timeout(self) -> None:
        self.assertEqual(
            pi_review.PI_REVIEW_BUDGET_SECONDS,
            pi_review.WORKFLOW_TIMEOUT_SECONDS
            - pi_review.WORKFLOW_RESERVE_SECONDS,
        )
        self.assertGreater(pi_review.WORKFLOW_RESERVE_SECONDS, 0)

    def test_prompt_states_runtime_and_per_finding_tool_limits(self) -> None:
        prompt = (SCRIPT_DIR / "pi_review_prompt.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("at most 16 total tool calls per diff chunk", prompt)
        self.assertIn("at most 4 tool calls per finding", prompt)


if __name__ == "__main__":
    unittest.main()
