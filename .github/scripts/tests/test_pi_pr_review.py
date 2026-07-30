from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
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
prompt="$(cat)"
{{
  printf 'home=%s\\n' "${{HOME:-}}"
  printf 'pi_dir=%s\\n' "${{PI_CODING_AGENT_DIR:-}}"
  printf 'offline=%s\\n' "${{PI_OFFLINE:-}}"
  printf 'token=%s\\n' "${{AGENT_FLEET_API_KEY:-}}"
  printf 'cwd=%s\\n' "$PWD"
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


def _make_text_response(text: str, *, stop_reason: str = "stop") -> str:
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
    return _make_text_response(text, stop_reason=stop_reason)


def _with_tool_calls(response: str, *tool_names: str) -> str:
    events = [json.loads(line) for line in response.splitlines()]
    events[3:3] = [
        {"type": "tool_execution_start", "toolName": name}
        for name in tool_names
    ]
    return "\n".join(json.dumps(event) for event in events)


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

    def test_strips_chat_completions_from_nonstandard_path(self) -> None:
        result = pi_review._chat_url_to_base(
            "https://gateway.example.com/v3/chat/completions"
        )
        self.assertEqual(result, "https://gateway.example.com/v3")

    def test_preserves_already_clean_url(self) -> None:
        result = pi_review._chat_url_to_base("https://api.example.com/v1")
        self.assertEqual(result, "https://api.example.com/v1")


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

    def test_final_fenced_object_after_prose(self) -> None:
        self.assertEqual(
            pi_review._extract_json(
                "Review complete.\n\n```json\n"
                '{"findings": []}\n```'
            ),
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

    def test_counts_tool_calls_in_valid_stream(self) -> None:
        result = pi_review._validate_pi_stream(
            _with_tool_calls(_make_findings_response([]), "grep", "read")
        )

        self.assertEqual(result["_pi_tool_calls"], 2)

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
        self.assertEqual(
            result,
            {
                "findings": [],
                "incomplete": True,
                "_pi_tool_calls": 0,
            },
        )

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
        self.repository_root = self.root / "repository"
        self.repository_root.mkdir()
        self.capture = self.bin_dir / "pi-capture.txt"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_client(self, **overrides) -> pi_review.PiClient:
        kwargs = {
            "pi_binary": str(self.bin_dir / "pi"),
            "base_url": "https://api.example.com/v1/chat/completions",
            "api_key": "test-api-key",
            "model": "test-model",
            "repository_root": self.repository_root,
            "timeout": 30,
        }
        kwargs.update(overrides)
        return pi_review.PiClient(**kwargs)

    def test_passes_system_prompt_and_model_input_to_pi(self) -> None:
        _stub_pi_script(self.bin_dir, stdout=_make_findings_response())
        client = self._make_client()

        client.review("You are a reviewer.", "FILE worker.py\n+stop()")

        captured = self.capture.read_text(encoding="utf-8")
        self.assertIn("prompt=<FILE worker.py\n+stop()>", captured)
        self.assertIn("arg=<--system-prompt>", captured)
        self.assertIn("arg=<You are a reviewer.>", captured)
        self.assertNotIn("arg=<--tools>", captured)
        self.assertNotIn("arg=<--no-tools>", captured)
        self.assertNotIn("arg=<--no-extensions>", captured)
        self.assertNotIn("arg=<--no-skills>", captured)
        self.assertNotIn("arg=<--no-context-files>", captured)
        self.assertIn("arg=<--approve>", captured)
        self.assertIn("arg=<--no-session>", captured)
        self.assertIn("offline=1", captured)
        self.assertIn(f"cwd={self.repository_root.resolve()}", captured)

    def test_sends_large_diff_through_stdin_not_argv(self) -> None:
        client = self._make_client()
        model_input = "x" * 200_000
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_findings_response([]),
            stderr="",
        )

        with mock.patch(
            "subprocess.run",
            return_value=completed,
        ) as run_mock:
            client.review("prompt", model_input)

        command = run_mock.call_args.args[0]
        self.assertLess(max(map(len, command)), 10_000)
        self.assertEqual(run_mock.call_args.kwargs["input"], model_input)

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
        client = self._make_client(timeout=30)
        with mock.patch(
            "time.monotonic",
            side_effect=[100.0, 100.0],
        ), mock.patch("subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(
                ["pi"], 30
            )
            with self.assertRaises(pi_review.PiReviewError) as ctx:
                client.review("prompt", "diff")
            self.assertIn("timed out", str(ctx.exception))
        run_mock.assert_called_once()

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

    def test_retries_one_malformed_model_response(self) -> None:
        client = self._make_client()
        malformed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_with_tool_calls(_make_text_response("not JSON"), "read"),
            stderr="",
        )
        valid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_with_tool_calls(
                _make_findings_response([]),
                "grep",
                "read",
            ),
            stderr="",
        )

        with mock.patch(
            "subprocess.run",
            side_effect=[malformed, valid],
        ) as run_mock:
            result = client.review(
                "prompt",
                "diff",
                retry_malformed=True,
            )

        self.assertEqual(result, {"findings": [], "_pi_tool_calls": 3})
        self.assertEqual(run_mock.call_count, 2)

    def test_malformed_retry_uses_remaining_timeout_budget(self) -> None:
        client = self._make_client(timeout=30)
        malformed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_text_response("not JSON"),
            stderr="",
        )
        valid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_findings_response([]),
            stderr="",
        )

        with mock.patch(
            "time.monotonic",
            side_effect=[100.0, 100.0, 112.5],
        ), mock.patch(
            "subprocess.run",
            side_effect=[malformed, valid],
        ) as run_mock:
            client.review("prompt", "diff", retry_malformed=True)

        timeouts = [call.kwargs["timeout"] for call in run_mock.call_args_list]
        self.assertEqual(timeouts, [30, 17.5])

    def test_malformed_retry_stops_when_timeout_budget_is_exhausted(self) -> None:
        client = self._make_client(timeout=30)
        malformed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_text_response("not JSON"),
            stderr="",
        )

        with mock.patch(
            "time.monotonic",
            side_effect=[100.0, 100.0, 130.0],
        ), mock.patch(
            "subprocess.run",
            return_value=malformed,
        ) as run_mock, self.assertRaises(pi_review.PiReviewError) as ctx:
            client.review("prompt", "diff", retry_malformed=True)

        self.assertIn("timed out after 30s", str(ctx.exception))
        run_mock.assert_called_once()

    def test_retries_one_schema_invalid_model_response(self) -> None:
        client = self._make_client()
        invalid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_with_tool_calls(
                _make_text_response('{"findings": {}}'),
                "read",
            ),
            stderr="",
        )
        valid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_with_tool_calls(
                _make_findings_response([]),
                "grep",
                "read",
            ),
            stderr="",
        )

        def validate(payload: dict) -> None:
            if not isinstance(payload.get("findings"), list):
                raise pi_review._review.ModelResponseError(
                    "findings must be an array"
                )

        with mock.patch(
            "subprocess.run",
            side_effect=[invalid, valid],
        ) as run_mock:
            result = client.review(
                "prompt",
                "diff",
                retry_malformed=True,
                response_validator=validate,
            )

        self.assertEqual(result, {"findings": [], "_pi_tool_calls": 3})
        self.assertEqual(run_mock.call_count, 2)

    def test_does_not_retry_malformed_model_response_by_default(self) -> None:
        client = self._make_client()
        malformed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_text_response("not JSON"),
            stderr="",
        )
        valid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_make_findings_response([]),
            stderr="",
        )

        with mock.patch(
            "subprocess.run",
            side_effect=[malformed, valid],
        ) as run_mock, self.assertRaises(pi_review.PiResponseFormatError):
            client.review("prompt", "diff")

        self.assertEqual(run_mock.call_count, 1)


# -- orchestration tests --------------------------------------------------


class FakeGitHub:
    def __init__(self) -> None:
        self.pull = {
            "draft": False,
            "head": {"sha": "head-1"},
            "base": {"sha": "base-1"},
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
        self.prompts: list[str] = []
        self.retry_malformed: list[bool] = []
        self.response_validators: list[object] = []
        self.attached_diff_paths: list[Path] = []
        self.attached_diffs: list[str] = []
        self.attached_diff_modes: list[int] = []

    def review(
        self,
        prompt: str,
        model_input: str,
        *,
        retry_malformed: bool = False,
        response_validator: object = None,
    ) -> dict:
        self.prompts.append(prompt)
        self.inputs.append(model_input)
        self.retry_malformed.append(retry_malformed)
        self.response_validators.append(response_validator)
        for line in model_input.splitlines():
            if line.startswith("UNTRUSTED DIFF FILE: "):
                diff_path = Path(
                    line.removeprefix("UNTRUSTED DIFF FILE: ")
                )
                self.attached_diff_paths.append(diff_path)
                self.attached_diffs.append(
                    diff_path.read_text(encoding="utf-8")
                )
                self.attached_diff_modes.append(
                    diff_path.stat().st_mode & 0o777
                )
        payload = {"findings": list(self.findings)}
        if callable(response_validator):
            response_validator(payload)
        return payload


class OrchestrationTest(unittest.TestCase):
    def test_fans_out_three_lenses_over_the_whole_diff(self) -> None:
        github = FakeGitHub()
        github.files.append(
            {
                "filename": "tests/test_worker.py",
                "patch": "@@ -0,0 +1 @@\n+test_stop()",
            }
        )
        pi_client = FakePiClient([])

        pi_review.run_review(github, pi_client, 7, "base prompt")

        self.assertEqual(len(pi_client.inputs), 3)
        for model_input in pi_client.inputs:
            self.assertIn("FILE worker.py", model_input)
            self.assertIn("FILE tests/test_worker.py", model_input)
        self.assertEqual(len(set(pi_client.prompts)), 3)
        self.assertTrue(all("base prompt" in prompt for prompt in pi_client.prompts))
        self.assertEqual(pi_client.retry_malformed, [True] * 3)
        self.assertTrue(all(callable(item) for item in pi_client.response_validators))

    def test_whole_diff_input_includes_all_files(self) -> None:
        sentinel = "SECOND_FILE_SENTINEL"
        github = FakeGitHub()
        github.files = [
            {
                "filename": f"large_{index}.py",
                "patch": (
                    "@@ -0,0 +1 @@\n+"
                    + (sentinel if index == 1 else f"file_{index}_")
                    + ("x" * 49_000)
                ),
            }
            for index in range(2)
        ]
        pi_client = FakePiClient([])

        pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(len(pi_client.inputs), 3)
        self.assertTrue(
            all(sentinel in model_input for model_input in pi_client.inputs)
        )

    def test_attaches_complete_token_dense_diff_with_bounded_input(self) -> None:
        github = FakeGitHub()
        github.files = [
            {
                "filename": "dense.py",
                "patch": "@@ -0,0 +1 @@\n+" + ("🧪" * 50_000),
            }
        ]
        pi_client = FakePiClient([])

        pi_review.run_review(github, pi_client, 7, "base prompt")

        self.assertTrue(
            all(
                len(model_input.encode("utf-8")) <= 120_000
                for model_input in pi_client.inputs
            )
        )
        self.assertEqual(len(pi_client.attached_diffs), 3)
        self.assertTrue(
            all(value.count("🧪") == 50_000 for value in pi_client.attached_diffs)
        )
        self.assertEqual(pi_client.attached_diff_modes, [0o400] * 3)
        self.assertNotIn(
            "Additional diff content exceeded",
            github.created[0][2],
        )
        self.assertTrue(
            all(not path.exists() for path in pi_client.attached_diff_paths)
        )

    def test_one_lens_failure_publishes_partial_results(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "trust boundaries" in prompt:
                raise pi_review.PiReviewError("provider detail must stay private")
            return {"findings": []}

        pi_client.review.side_effect = review_lens

        result = pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(result, "published")
        body = github.created[0][2]
        self.assertIn("Coverage: Partial", body)
        self.assertIn("Failed lenses: security", body)
        self.assertNotIn("provider detail", body)

    def test_partial_review_is_terminal_for_revision(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def fail_security(
            prompt: str,
            _model_input: str,
            **_kwargs: object,
        ) -> dict:
            if "trust boundaries" in prompt:
                raise pi_review.PiReviewError("failed")
            return {"findings": []}

        pi_client.review.side_effect = fail_security
        first = pi_review.run_review(github, pi_client, 7, "{{LENS}}")
        partial_body = github.created[0][2]
        github.reviews = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": partial_body,
            }
        ]
        pi_client.reset_mock()

        second = pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual((first, second), ("published", "duplicate"))
        self.assertIn("<!-- pi-pr-review:head-1 -->", partial_body)
        self.assertEqual(len(github.created), 1)
        pi_client.review.assert_not_called()

    def test_fallback_prompt_requests_only_inline_findings(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])

        with mock.patch.object(
            pi_review,
            "_shared_routing_available",
            return_value=False,
        ):
            pi_review.run_review(
                github,
                pi_client,
                7,
                "base prompt",
            )

        self.assertTrue(
            all("added RIGHT-side line" in prompt for prompt in pi_client.prompts)
        )
        self.assertTrue(
            all("set line to null" not in prompt for prompt in pi_client.prompts)
        )

    def test_shared_router_prompt_requests_unanchorable_findings(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])

        with (
            mock.patch.object(
                pi_review._review,
                "parse_findings",
                side_effect=lambda _payload: ([], 0),
                create=True,
            ),
            mock.patch.object(
                pi_review._review,
                "route_findings",
                side_effect=lambda _findings, _files: ([], []),
                create=True,
            ),
            mock.patch.object(
                pi_review._review,
                "build_summary",
                return_value="summary",
            ),
        ):
            pi_review.run_review(
                github,
                pi_client,
                7,
                "base prompt",
            )

        self.assertTrue(
            all("set line to null" in prompt for prompt in pi_client.prompts)
        )
        self.assertTrue(
            all("contextual unchanged lines" in prompt for prompt in pi_client.prompts)
        )
        self.assertTrue(
            all(
                "overrides the prompt's default" in prompt
                for prompt in pi_client.prompts
            )
        )

    def test_two_lens_failures_publish_the_remaining_result(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "runtime correctness" in prompt:
                return {"findings": []}
            raise pi_review.PiReviewError("failed")

        pi_client.review.side_effect = review_lens

        result = pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(result, "published")
        body = github.created[0][2]
        self.assertIn("Failed lenses: security, tests/regression", body)

    def test_reports_tool_calls_by_lens(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "runtime correctness" in prompt:
                count = 1
            elif "trust boundaries" in prompt:
                count = 2
            else:
                count = 3
            return {"findings": [], "_pi_tool_calls": count}

        pi_client.review.side_effect = review_lens

        pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        body = github.created[0][2]
        self.assertIn(
            "Tool calls by lens: correctness=1, security=2, "
            "tests/regression=3",
            body,
        )

    def test_all_lens_failures_fail_without_publishing(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()
        pi_client.review.side_effect = pi_review.PiReviewError("failed")

        with self.assertRaisesRegex(
            pi_review.PiReviewError,
            "all review lenses failed",
        ):
            pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(github.created, [])

    def test_merges_overlapping_findings_at_the_highest_severity(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "runtime correctness" in prompt:
                severity = "P2"
                title = "Cancellation cleanup missing"
            elif "trust boundaries" in prompt:
                severity = "P1"
                title = "Cancellation cleanup missing"
            else:
                severity = "P2"
                title = "Missing cancellation cleanup"
            return {
                "findings": [
                    {
                        "severity": severity,
                        "path": "worker.py",
                        "line": 2,
                        "title": title,
                        "failure_scenario": "Worker survives wrapper.",
                        "remediation": "Terminate the process group.",
                    }
                ]
            }

        pi_client.review.side_effect = review_lens

        pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        findings = github.created[0][3]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "P1")

    def test_merged_finding_retains_all_lens_attribution(self) -> None:
        @dataclass(frozen=True)
        class AttributedFinding:
            severity: str
            path: str
            line: int
            title: str
            failure_scenario: str
            remediation: str
            lenses: tuple[str, ...] = ()

        github = FakeGitHub()

        with mock.patch.object(
            pi_review._review,
            "Finding",
            AttributedFinding,
        ):
            pi_review.run_review(github, FakePiClient(), 7, "{{LENS}}")

        findings = github.created[0][3]
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0].lenses,
            ("correctness", "security", "tests/regression"),
        )

    def test_distinct_findings_on_one_line_remain_separate(self) -> None:
        findings = [
            pi_review._review.Finding(
                "P2",
                "worker.py",
                2,
                title,
                "Failure",
                "Fix",
            )
            for title in (
                "Cancellation leak",
                "Credential exposure",
                "Missing regression coverage",
            )
        ]

        merged = pi_review.merge_lens_findings(findings)

        self.assertEqual(len(merged), 3)

    def test_generic_title_overlap_does_not_merge_distinct_failures(self) -> None:
        findings = [
            pi_review._review.Finding(
                "P1",
                "worker.py",
                2,
                "Missing authorization check",
                "An unauthenticated caller can delete another user's job.",
                "Require ownership before deletion.",
            ),
            pi_review._review.Finding(
                "P2",
                "worker.py",
                2,
                "Missing null check",
                "An absent worker result raises AttributeError during cleanup.",
                "Handle the absent result before dereferencing it.",
            ),
        ]

        merged = pi_review.merge_lens_findings(findings)

        self.assertEqual(len(merged), 2)

    def test_generic_wording_does_not_merge_distinct_subjects(self) -> None:
        findings = [
            pi_review._review.Finding(
                "P1",
                "worker.py",
                2,
                "Reject missing owner check",
                "A request missing owner deletes the job.",
                "Require ownership before deletion.",
            ),
            pi_review._review.Finding(
                "P1",
                "worker.py",
                2,
                "Reject missing payload check",
                "A request missing payload crashes the job.",
                "Validate the payload before use.",
            ),
        ]

        merged = pi_review.merge_lens_findings(findings)

        self.assertEqual(len(merged), 2)

    def test_filler_words_do_not_prevent_equivalent_findings_from_merging(
        self,
    ) -> None:
        findings = [
            pi_review._review.Finding(
                "P2",
                "worker.py",
                2,
                title,
                "Worker survives wrapper.",
                "Terminate the process group.",
            )
            for title in (
                "Missing cancellation cleanup",
                "Cancellation cleanup is missing",
            )
        ]

        merged = pi_review.merge_lens_findings(findings)

        self.assertEqual(len(merged), 1)

    def test_caps_merged_inline_findings_across_lenses(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "runtime correctness" in prompt:
                prefix = "correctness"
            elif "trust boundaries" in prompt:
                prefix = "security"
            else:
                prefix = "regression"
            return {
                "findings": [
                    {
                        "severity": "P2",
                        "path": "worker.py",
                        "line": 2,
                        "title": f"{prefix}{index}",
                        "failure_scenario": f"{prefix}{index} fails.",
                        "remediation": f"Fix {prefix}{index}.",
                    }
                    for index in range(10)
                ]
            }

        pi_client.review.side_effect = review_lens

        pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(len(github.created[0][3]), pi_review._review.MAX_COMMENTS)
        body = github.created[0][2]
        if pi_review._shared_routing_available():
            self.assertIn("Rejected model findings: 0", body)
            self.assertIn("security0", body)
        else:
            self.assertIn(
                "Automated review found 20 actionable finding(s).",
                body,
            )
            self.assertIn("Rejected model findings: 10", body)

    def test_applies_inline_cap_after_deduplicating_each_lens(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()
        overlapping = [
            {
                "severity": "P2",
                "path": "worker.py",
                "line": 2,
                "title": title,
                "failure_scenario": "Worker survives wrapper.",
                "remediation": "Terminate the process group.",
            }
            for title in (
                "Cancellation cleanup missing",
                "Missing cancellation cleanup",
            )
        ]
        distinct = [
            {
                "severity": "P2",
                "path": "worker.py",
                "line": 2,
                "title": f"defect{index}",
                "failure_scenario": f"scenario{index}",
                "remediation": f"fix{index}",
            }
            for index in range(2, 20)
        ]
        sentinel = {
            "severity": "P2",
            "path": "worker.py",
            "line": 2,
            "title": "overflow-sentinel",
            "failure_scenario": "sentinel-scenario",
            "remediation": "sentinel-fix",
        }

        def review_lens(
            prompt: str, _model_input: str, **_kwargs: object
        ) -> dict:
            if "runtime correctness" in prompt:
                return {"findings": overlapping + distinct + [sentinel]}
            return {"findings": []}

        pi_client.review.side_effect = review_lens

        with mock.patch.object(
            pi_review,
            "_shared_routing_available",
            return_value=False,
        ):
            pi_review.run_review(github, pi_client, 7, "base prompt")

        findings = github.created[0][3]
        self.assertEqual(len(findings), pi_review._review.MAX_COMMENTS)
        self.assertIn("overflow-sentinel", [item.title for item in findings])

    def test_uses_shared_routing_contract_when_available(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient([])
        findings = [
            pi_review._review.Finding(
                "P1", "worker.py", 2, "Inline", "Failure", "Fix"
            ),
            pi_review._review.Finding(
                "P3", "worker.py", 2, "Minor", "Failure", "Fix"
            ),
            pi_review._review.Finding(
                "P2", "helper.py", None, "Other", "Failure", "Fix"
            ),
        ]

        def route(items: list, _files: dict) -> tuple[list, list]:
            return [items[0]], items[1:]

        with (
            mock.patch.object(
                pi_review._review,
                "parse_findings",
                return_value=(findings, 0),
                create=True,
            ),
            mock.patch.object(
                pi_review._review,
                "route_findings",
                side_effect=route,
                create=True,
            ),
            mock.patch.object(
                pi_review._review,
                "build_summary",
                return_value="routed summary",
            ) as build_summary,
        ):
            pi_review.run_review(github, pi_client, 7, "{{LENS}}")

        self.assertEqual(
            [item.title for item in github.created[0][3]],
            ["Inline"],
        )
        summary_findings = build_summary.call_args.kwargs["summary_findings"]
        self.assertEqual(
            [item.title for item in summary_findings],
            ["Other", "Minor"],
        )

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

    def test_same_head_on_a_different_base_is_reviewed(self) -> None:
        github = FakeGitHub()
        github.reviews = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- pi-pr-review:head-1 -->",
            },
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- pi-pr-review:head-1:base-0 -->",
            },
        ]

        result = pi_review.run_review(
            github,
            FakePiClient([]),
            7,
            "prompt",
            expected_head_sha="head-1",
            expected_base_sha="base-1",
        )

        self.assertEqual(result, "published")
        self.assertIn(
            "<!-- pi-pr-review:head-1:base-1 -->",
            github.created[0][2],
        )

    def test_same_head_and_base_is_duplicate(self) -> None:
        github = FakeGitHub()
        github.reviews = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- pi-pr-review:head-1:base-1 -->",
            }
        ]

        result = pi_review.run_review(
            github,
            FakePiClient([]),
            7,
            "prompt",
            expected_head_sha="head-1",
            expected_base_sha="base-1",
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

    def test_event_revision_mismatch_skips_before_model(self) -> None:
        github = FakeGitHub()
        pi_client = FakePiClient()

        result = pi_review.run_review(
            github,
            pi_client,
            7,
            "prompt",
            expected_head_sha="head-2",
            expected_base_sha="base-1",
        )

        self.assertEqual(result, "stale")
        self.assertEqual(pi_client.inputs, [])
        self.assertEqual(github.created, [])

    def test_event_base_change_is_not_published(self) -> None:
        github = FakeGitHub()
        first = dict(github.pull)
        second = {
            **github.pull,
            "base": {"sha": "base-2"},
        }
        github.get_pull = mock.Mock(side_effect=[first, second])

        result = pi_review.run_review(
            github,
            FakePiClient(),
            7,
            "prompt",
            expected_head_sha="head-1",
            expected_base_sha="base-1",
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

    def test_incomplete_lens_is_reported(self) -> None:
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
        self.assertIn("review lens(es)", body)

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

    def test_all_invalid_lens_payloads_fail_the_review(self) -> None:
        github = FakeGitHub()
        pi_client = mock.Mock()
        pi_client.review.return_value = {"findings": "invalid"}

        with self.assertRaises(pi_review.PiReviewError) as ctx:
            pi_review.run_review(github, pi_client, 7, "prompt")

        self.assertIn("all review lenses failed", str(ctx.exception))


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

    def test_pi_timeout_is_longer_than_raw_api(self) -> None:
        self.assertGreater(
            pi_review.PI_TIMEOUT_SECONDS, 600,
            "agent review should allow more time than direct API calls",
        )

    def test_prompt_defaults_to_inline_routing_without_placeholders(self) -> None:
        prompt = " ".join(
            SCRIPT_DIR.joinpath("pi_review_prompt.md").read_text().split()
        )

        self.assertIn("added RIGHT-side line", prompt)
        self.assertNotIn("{{ROUTING}}", prompt)
        self.assertNotIn("{{LENS}}", prompt)

    def test_main_binds_review_to_event_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            prompt_path = root / "prompt.md"
            event = {
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "event-head"},
                    "base": {"sha": "event-base"},
                }
            }
            event_path.write_text(
                json.dumps(event),
                encoding="utf-8",
            )
            prompt_path.write_text("review prompt", encoding="utf-8")
            args = mock.Mock(
                event_path=event_path,
                prompt_path=prompt_path,
                pi_bin="pi",
            )
            environment = {
                "GITHUB_REPOSITORY": "owner/repository",
                "GITHUB_TOKEN": "fake-github-token",
                "GITHUB_WORKSPACE": str(root),
                "LLM_REVIEW_API_KEY": "fake-model-key",
                "LLM_REVIEW_BASE_URL": "https://example.com/v1",
                "LLM_REVIEW_MODEL": "test-model",
            }
            with (
                mock.patch.object(
                    pi_review, "parse_args", return_value=args
                ),
                mock.patch.object(
                    pi_review,
                    "require_env",
                    side_effect=environment.__getitem__,
                ),
                mock.patch.object(
                    pi_review, "run_review", return_value="stale"
                ) as run_mock,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(pi_review.main(), 0)

        self.assertEqual(
            run_mock.call_args.kwargs,
            {
                "review_id": pi_review.PI_REVIEW_ID,
                "expected_head_sha": "event-head",
                "expected_base_sha": "event-base",
            },
        )


if __name__ == "__main__":
    unittest.main()
