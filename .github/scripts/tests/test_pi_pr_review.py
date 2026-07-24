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
from contextlib import redirect_stderr
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
    def test_real_pi_uses_adapted_chat_completions_path(self) -> None:
        pi_binary = shutil.which("pi")
        if pi_binary is None:
            self.skipTest(
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
            self.skipTest(
                "pi runtime integration requires pi 0.81.1; "
                f"pi --version reported {version or 'no version'}"
            )

        model = "integration-model"
        chunks = [
            {
                "id": "chatcmpl-integration",
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
                "id": "chatcmpl-integration",
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
        response_body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        ).encode()
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(
                    {
                        "path": self.path,
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
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
            with tempfile.TemporaryDirectory() as temp_dir:
                repository_root = Path(temp_dir) / "repository"
                repository_root.mkdir()
                port = server.server_address[1]
                client = pi_review.PiClient(
                    pi_binary=pi_binary,
                    base_url=(
                        f"http://127.0.0.1:{port}"
                        "/custom/chat/completions"
                    ),
                    api_key="integration-api-key",
                    model=model,
                    repository_root=repository_root,
                    timeout=30,
                )
                with mock.patch.dict(
                    os.environ,
                    {
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    },
                ):
                    result = client.review(
                        (
                            "Return exactly one JSON object with an empty "
                            "findings array. No tools are needed."
                        ),
                        "Return the requested JSON now.",
                    )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

        self.assertFalse(server_thread.is_alive())
        self.assertEqual(result, {"findings": []})
        self.assertEqual(
            [request["path"] for request in requests],
            ["/custom/chat/completions"],
        )
        self.assertEqual(requests[0]["body"]["model"], model)
        self.assertTrue(requests[0]["body"]["stream"])


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
    def __init__(self, findings: list[dict] | None = None) -> None:
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

    def review(self, _prompt: str, chunk: str) -> dict:
        self.inputs.append(chunk)
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


if __name__ == "__main__":
    unittest.main()
