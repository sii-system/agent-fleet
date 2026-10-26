import contextlib
import importlib.util
import io
import json
import os
import tempfile
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

    def test_checks_benchmark_models_without_replacing_them_with_agent_model(self):
        for benchmark, expected in (
            ("scale-ai/swe-atlas-qna", "anthropic/claude-opus-4-5-20251101"),
            ("sierra-research/tau3-bench", "gpt-5.2"),
        ):
            with (
                self.subTest(benchmark=benchmark),
                mock.patch.dict(os.environ, {
                    "MODEL": "fake-agent-model", "API_KEY": "fake-key",
                    "BASE_URL_RAW": "https://agent.invalid",
                }, clear=True),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(preflight.main(["--benchmark", benchmark]), 0)
                self.assertEqual(probe.call_args_list, [mock.call(
                    "https://agent.invalid/v1", expected, "fake-key", "chat/completions"
                )])

    def test_separate_gateway_is_published_only_after_successful_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            env = {
                "API_KEY": "fake-agent-key", "BASE_URL_RAW": "https://agent.invalid",
                "VERIFIER_BASE_URL_RAW": "https://judge.invalid/v1/chat/completions",
                "VERIFIER_API_KEY": "fake-judge-key", "GITHUB_ENV": str(target),
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(preflight, "probe", side_effect=preflight.PreflightError("HTTP 404")),
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(preflight.main(["--benchmark", "scale-ai/swe-atlas-qna"]), 1)
                self.assertFalse(target.exists())
                self.assertNotIn("fake-judge-key", error.getvalue())
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(preflight.main(["--benchmark", "scale-ai/swe-atlas-qna"]), 0)
                self.assertEqual(probe.call_args.args[:3], (
                    "https://judge.invalid/v1", "anthropic/claude-opus-4-5-20251101", "fake-judge-key"
                ))
                self.assertEqual(target.read_text(), (
                    "OPENAI_BASE_URL=https://judge.invalid/v1\n"
                    "OPENAI_API_BASE=https://judge.invalid/v1\nOPENAI_API_KEY=fake-judge-key\n"
                ))
                self.assertNotIn("fake-judge-key", output.getvalue())

    def test_partial_gateway_configuration_fails_without_requests(self):
        with (
            mock.patch.dict(os.environ, {"VERIFIER_BASE_URL_RAW": "https://judge.invalid"}, clear=True),
            mock.patch.object(preflight, "probe") as probe,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(preflight.main(["--benchmark", "scale-ai/swe-atlas-qna"]), 1)
            probe.assert_not_called()

    def test_unrelated_benchmarks_need_no_additional_models(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(preflight, "probe") as probe:
            self.assertEqual(preflight.main(["--benchmark", "seta"]), 0)
            probe.assert_not_called()

    def test_rejects_credential_newlines_before_writing_environment(self):
        with (
            mock.patch.dict(os.environ, {
                "VERIFIER_BASE_URL_RAW": "https://judge.invalid",
                "VERIFIER_API_KEY": "fake-key\nINJECTED=value",
            }, clear=True),
            mock.patch.object(preflight, "probe") as probe,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(preflight.main(["--benchmark", "scale-ai/swe-atlas-qna"]), 1)
            probe.assert_not_called()
            self.assertNotIn("INJECTED", output.getvalue())

    def test_gpt5_dependency_uses_completion_token_budget(self):
        with mock.patch.object(preflight, "urlopen", return_value=self.response({"choices": [{}]})) as request:
            preflight.probe("https://judge.invalid/v1", "gpt-5.2", "fake-key", "chat/completions")
            body = json.loads(request.call_args.args[0].data)
            self.assertEqual(body["max_completion_tokens"], 16)
            self.assertNotIn("max_tokens", body)

    def test_run_profile_publishes_the_run_gateway_and_role_models(self):
        for benchmark, expected_models in (
            ("scale-ai/swe-atlas-qna", {"EVAL_MODEL": "run-model"}),
            ("sierra-research/tau3-bench", {
                "TAU2_USER_MODEL": "openai/run-model",
                "TAU2_NL_ASSERTIONS_MODEL": "openai/run-model",
                "TAU2_USER_LLM_ARGS_JSON": "{}",
            }),
        ):
            with (
                self.subTest(benchmark=benchmark), tempfile.TemporaryDirectory() as tmp,
                mock.patch.dict(os.environ, {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL": "https://run.invalid/v1/chat/completions",
                    "BASE_URL_RAW": "https://stale.invalid",
                    "OPENAI_BASE_URL": "https://stale.invalid",
                    "OPENAI_API_KEY": "fake-stale-key",
                    "GITHUB_ENV": str(Path(tmp) / "github-env"),
                }, clear=True),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", benchmark, "--benchmark-model-source", "run"
                ]), 0)
                probe.assert_called_once_with(
                    "https://run.invalid/v1", "run-model", "fake-run-key", "chat/completions"
                )
                exported = dict(line.split("=", 1) for line in
                                (Path(tmp) / "github-env").read_text().splitlines())
                self.assertEqual(exported, {
                    "OPENAI_BASE_URL": "https://run.invalid/v1",
                    "OPENAI_API_BASE": "https://run.invalid/v1",
                    "OPENAI_API_KEY": "fake-run-key", **expected_models,
                })
                self.assertNotIn("fake-run-key", output.getvalue())

    def test_run_profile_publishes_nothing_when_a_model_check_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            target.write_text("EXISTING=value\n")
            with (
                mock.patch.dict(os.environ, {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL_RAW": "https://run.invalid",
                    "GITHUB_ENV": str(target),
                    "TAU2_NL_ASSERTIONS_MODEL": "openai/other-judge",
                }, clear=True),
                mock.patch.object(preflight, "probe", side_effect=[
                    None, preflight.PreflightError("HTTP 404")
                ]) as probe,
                contextlib.redirect_stderr(io.StringIO()) as output,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", "sierra-research/tau3-bench", "--benchmark-model-source", "run"
                ]), 1)
                self.assertEqual([call.args[1] for call in probe.call_args_list],
                                 ["run-model", "other-judge"])
                self.assertEqual(target.read_text(), "EXISTING=value\n")
                self.assertNotIn("fake-run-key", output.getvalue())

    def test_run_profile_preserves_explicit_role_models(self):
        with (
            mock.patch.dict(os.environ, {
                "MODEL": "run-model", "API_KEY": "fake-run-key",
                "BASE_URL_RAW": "https://run.invalid", "EVAL_MODEL": "custom-judge",
            }, clear=True),
            mock.patch.object(preflight, "probe") as probe,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(preflight.main([
                "--benchmark", "scale-ai/swe-atlas-qna", "--benchmark-model-source", "run"
            ]), 0)
            self.assertEqual(probe.call_args.args[1], "custom-judge")

    def test_run_profile_preserves_explicit_tau_user_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            with (
                mock.patch.dict(os.environ, {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL": "https://run.invalid", "GITHUB_ENV": str(target),
                    "TAU2_USER_REASONING_EFFORT": "high", "TAU2_USER_TEMPERATURE": "0.2",
                }, clear=True),
                mock.patch.object(preflight, "probe"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", "sierra-research/tau3-bench", "--benchmark-model-source", "run"
                ]), 0)
            exported = dict(line.split("=", 1) for line in target.read_text().splitlines())
            self.assertEqual(json.loads(exported["TAU2_USER_LLM_ARGS_JSON"]),
                             {"reasoning_effort": "high", "temperature": 0.2})

    def test_run_profile_preserves_explicit_tau_argument_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            env = {
                "MODEL": "run-model", "API_KEY": "fake-run-key",
                "BASE_URL": "https://run.invalid", "GITHUB_ENV": str(target),
                "TAU2_USER_LLM_ARGS_JSON": '{"temperature":0.3}',
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(preflight, "probe"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", "sierra-research/tau3-bench", "--benchmark-model-source", "run"
                ]), 0)
            exported = dict(line.split("=", 1) for line in target.read_text().splitlines())
            effective = env | exported
            self.assertEqual(effective["TAU2_USER_LLM_ARGS_JSON"], env["TAU2_USER_LLM_ARGS_JSON"])

    def test_run_profile_rejects_invalid_tau_temperature_before_publish(self):
        for temperature in ("not-numeric", "nan"):
            with (
                self.subTest(temperature=temperature), tempfile.TemporaryDirectory() as tmp,
                mock.patch.dict(os.environ, {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL": "https://run.invalid", "GITHUB_ENV": str(Path(tmp) / "github-env"),
                    "TAU2_USER_TEMPERATURE": temperature,
                }, clear=True),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", "sierra-research/tau3-bench", "--benchmark-model-source", "run"
                ]), 1)
                probe.assert_not_called()
                self.assertFalse((Path(tmp) / "github-env").exists())

    def test_run_profile_rejects_missing_model_and_unsafe_role_values(self):
        for setting in ({"MODEL": ""}, {"EVAL_MODEL": ""},
                        {"EVAL_MODEL": "model\nINJECTED=value"}):
            with (
                self.subTest(setting=setting),
                mock.patch.dict(os.environ, {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL_RAW": "https://run.invalid", **setting,
                }, clear=True),
                mock.patch.object(preflight, "probe") as probe,
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(preflight.main([
                    "--benchmark", "scale-ai/swe-atlas-qna", "--benchmark-model-source", "run"
                ]), 1)
                probe.assert_not_called()
                self.assertNotIn("INJECTED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
