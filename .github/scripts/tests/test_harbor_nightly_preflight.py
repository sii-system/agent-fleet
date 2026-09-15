import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "harbor_nightly_preflight", ROOT / ".github/scripts/harbor_nightly_preflight.py"
)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class PreflightTest(unittest.TestCase):
    def response(self, data):
        return io.BytesIO(json.dumps(data).encode())

    def test_normalizes_the_same_gateway_forms_as_harbor(self):
        for path in ("", "/", "/v1", "/v1/", "/v1/chat/completions/"):
            self.assertEqual(preflight.api_root("https://gateway.invalid" + path),
                             "https://gateway.invalid/v1")

    def test_rejects_invalid_urls_without_echoing_credentials(self):
        for url in ("", "file:///tmp/model", "https://secret@gateway.invalid/v1",
                    "https://gateway.invalid/v1?key=secret"):
            with self.assertRaises(preflight.PreflightError) as error:
                preflight.api_root(url)
            self.assertNotIn("secret", str(error.exception))

    def test_checks_both_routes_for_claude_and_only_chat_for_opencode(self):
        for agent, routes in (("claude-code", ["chat/completions", "messages"]),
                              ("opencode", ["chat/completions"])):
            with (
                mock.patch.dict(os.environ, {"MODEL": "fake-model", "API_KEY": "fake-key",
                                             "BASE_URL_RAW": "https://gateway.invalid/v1"}),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(preflight.main(["--agent", agent]), 0)
                self.assertEqual([call.args[-1] for call in probe.call_args_list], routes)

    def test_sends_bounded_completions_with_the_selected_model_and_auth(self):
        for route in ("chat/completions", "messages"):
            response = {"type": "message"} if route == "messages" else {"choices": [{}]}
            with mock.patch.object(preflight, "urlopen", return_value=self.response(response)) as request:
                preflight.probe("https://gateway.invalid/v1", "fake-model", "fake-key", route)
                req = request.call_args.args[0]
                self.assertEqual(req.full_url, "https://gateway.invalid/v1/" + route)
                body = json.loads(req.data)
                self.assertEqual(body["model"], "fake-model")
                self.assertEqual(body["max_tokens"], 16)
                self.assertFalse(body["stream"])
                self.assertEqual(req.get_header("Authorization"), "Bearer fake-key")
                self.assertEqual(request.call_args.kwargs["timeout"], 20)
                if route == "messages":
                    self.assertEqual(req.get_header("X-api-key"), "fake-key")
                    self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")

    def test_permanent_errors_fail_immediately_without_leaking_server_body(self):
        for status in (400, 401, 403, 404):
            error = HTTPError("https://gateway.invalid", status, "secret", {}, io.BytesIO(b"secret"))
            with mock.patch.object(preflight, "urlopen", side_effect=error) as request:
                with self.assertRaisesRegex(preflight.PreflightError, f"HTTP {status}") as caught:
                    preflight.probe("https://gateway.invalid", "fake-model", "fake-key", "messages")
                self.assertEqual(request.call_count, 1)
                self.assertNotIn("secret", str(caught.exception))

    def test_transient_failures_recover_with_bounded_backoff(self):
        for error in (URLError("secret"), TimeoutError("secret"),
                      HTTPError("https://gateway.invalid", 429, "busy", {}, None),
                      HTTPError("https://gateway.invalid", 503, "down", {}, None)):
            with (
                mock.patch.object(preflight, "urlopen", side_effect=[error, self.response({"choices": [{}]})]) as request,
                mock.patch.object(preflight.time, "sleep") as sleep,
            ):
                preflight.probe("https://gateway.invalid", "fake-model", "fake-key", "chat/completions")
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_persistent_connection_failures_stop_after_three_attempts(self):
        with (
            mock.patch.object(preflight, "urlopen", side_effect=URLError("secret")) as request,
            mock.patch.object(preflight.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(preflight.PreflightError, "after 3 attempts"):
                preflight.probe("https://gateway.invalid", "fake-model", "fake-key", "messages")
            self.assertEqual(request.call_count, 3)
            self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_rejects_error_payloads_even_with_http_success(self):
        for data in ({"error": "secret"}, {}, [], None):
            with (
                mock.patch.object(preflight, "urlopen", return_value=self.response(data)),
                self.assertRaisesRegex(preflight.PreflightError, "invalid completion"),
            ):
                preflight.probe("https://gateway.invalid", "fake-model", "fake-key", "messages")

    def test_rejects_non_json_without_echoing_response(self):
        with (
            mock.patch.object(preflight, "urlopen", return_value=io.BytesIO(b"secret")),
            self.assertRaisesRegex(preflight.PreflightError, "not valid JSON"),
        ):
            preflight.probe("https://gateway.invalid", "fake-model", "fake-key", "messages")

    def test_missing_credentials_do_not_make_requests(self):
        with (
            mock.patch.dict(os.environ, {"MODEL": "fake-model", "API_KEY": ""}),
            mock.patch.object(preflight, "probe") as probe,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(preflight.main([]), 1)
            probe.assert_not_called()
            self.assertIn("::error::", output.getvalue())

    def test_workflows_check_the_same_model_before_setup_and_trials(self):
        for name in ("harbor-e2e-validation.yml", "harbor-self-hosted-nightly.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text()
            step = workflow.split("      - name: Check nightly model availability\n")[1].split("      - name:")[0]
            self.assertIn("timeout-minutes: 3", step)
            self.assertIn("harbor_nightly_preflight.py", step)
            self.assertLess(workflow.index("Check nightly model availability"),
                            workflow.index("Validate prerequisites"))
            self.assertEqual(workflow.count("MODEL: ${{ vars.HARBOR_NIGHTLY_MODEL || vars.LLM_REVIEW_MODEL }}"), 2)


if __name__ == "__main__":
    unittest.main()
