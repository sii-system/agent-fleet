from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import llm_pr_review as review


class PatchParsingTest(unittest.TestCase):
    def test_parse_patch_maps_only_added_right_lines(self) -> None:
        patch = """@@ -10,3 +10,4 @@
 context
-old
+new
+extra
 tail
"""
        parsed = review.parse_patch("src/example.py", patch)

        self.assertEqual(parsed.right_lines, frozenset({11, 12}))
        self.assertIn("RIGHT 11", parsed.review_text)
        self.assertIn("RIGHT 12", parsed.review_text)

    def test_parse_patch_tracks_multiple_hunks(self) -> None:
        patch = """@@ -1,1 +1,2 @@
 one
+two
@@ -20,2 +21,2 @@
-old
+new
 keep
"""
        parsed = review.parse_patch("src/example.py", patch)

        self.assertEqual(parsed.right_lines, frozenset({2, 21}))

    def test_skip_reason_is_explicit_and_deterministic(self) -> None:
        cases = {
            "Agents/Openclaw/docker-compose.yml": "generated",
            "web/app.min.js": "generated",
            "web/app.min.css": "generated",
            "web/app.map": "generated",
            "package-lock.json": "lockfile",
            "nested/pnpm-lock.yaml": "lockfile",
            "nested/yarn.lock": "lockfile",
            "uv.lock": "lockfile",
        }

        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(review.skip_reason(path, "normal patch"), expected)

    def test_missing_binary_submodule_and_oversized_patches_are_skipped(self) -> None:
        self.assertEqual(review.skip_reason("image.png", None), "binary-or-missing")
        self.assertEqual(
            review.skip_reason(
                "vendor/module",
                "@@ -1 +1 @@\n-Subproject commit abc123\n+Subproject commit def456",
            ),
            "submodule",
        )
        self.assertEqual(
            review.skip_reason("src/huge.py", "x" * 60_001),
            "oversized",
        )


class ModelContractTest(unittest.TestCase):
    def test_build_chunks_respects_total_and_chunk_budgets(self) -> None:
        files = [
            review.ParsedFile("a.py", "A" * 30_000, frozenset({1})),
            review.ParsedFile("b.py", "B" * 30_000, frozenset({2})),
        ]

        chunks, truncated = review.build_chunks(
            files, max_chunk_chars=50_000, max_total_chars=55_000
        )

        self.assertTrue(all(len(chunk) <= 50_000 for chunk in chunks))
        self.assertEqual(sum(map(len, chunks)), 55_000)
        self.assertTrue(truncated)

    def test_build_chunks_repeats_file_header_after_a_split(self) -> None:
        file = review.ParsedFile(
            "large.py",
            "FILE large.py\n" + ("x" * 100),
            frozenset({1}),
        )

        chunks, truncated = review.build_chunks(
            [file], max_chunk_chars=50, max_total_chars=200
        )

        self.assertFalse(truncated)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("FILE large.py\n") for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 50 for chunk in chunks))

    def test_build_chunks_split_at_rendered_line_boundaries(self) -> None:
        lines = [f"+ RIGHT {line}: value-{line}\n" for line in range(1, 10)]
        file = review.ParsedFile(
            "lines.py",
            "FILE lines.py\n" + "".join(lines),
            frozenset(range(1, 10)),
        )

        chunks, truncated = review.build_chunks(
            [file], max_chunk_chars=60, max_total_chars=1_000
        )

        self.assertFalse(truncated)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            body = chunk.removeprefix("FILE lines.py\n")
            self.assertTrue(body.endswith("\n"))
            for line in body.splitlines():
                self.assertRegex(line, r"^\+ RIGHT \d+: value-\d+$")

    def test_extract_json_accepts_fenced_object_and_rejects_trailing_text(self) -> None:
        self.assertEqual(
            review.extract_json('```json\n{"findings": []}\n```'),
            {"findings": []},
        )
        with self.assertRaises(review.ModelResponseError):
            review.extract_json('{"findings": []} ignore this')

    def test_extract_json_accepts_bare_fenced_object(self) -> None:
        self.assertEqual(
            review.extract_json('```\n{"findings": []}\n```'),
            {"findings": []},
        )

    def test_validate_findings_rejects_non_right_lines_and_sorts_severity(
        self,
    ) -> None:
        parsed = {
            "src/a.py": review.ParsedFile("src/a.py", "", frozenset({8, 9}))
        }
        payload = {
            "findings": [
                {
                    "severity": "P2",
                    "path": "src/a.py",
                    "line": 9,
                    "title": "Missing regression coverage",
                    "failure_scenario": "The changed branch is not exercised.",
                    "remediation": "Add a focused test for the new branch.",
                },
                {
                    "severity": "P0",
                    "path": "src/a.py",
                    "line": 7,
                    "title": "Invalid anchor",
                    "failure_scenario": "This is not an added line.",
                    "remediation": "Do not publish this finding.",
                },
                {
                    "severity": "P1",
                    "path": "src/a.py",
                    "line": 8,
                    "title": "Worker survives cancellation",
                    "failure_scenario": "The child remains alive after TERM.",
                    "remediation": "Terminate the process group.",
                },
            ]
        }

        findings, rejected = review.validate_findings(payload, parsed)

        self.assertEqual([item.severity for item in findings], ["P1", "P2"])
        self.assertEqual(rejected, 1)

    def test_validation_deduplicates_and_caps_findings(self) -> None:
        parsed = {
            "a.py": review.ParsedFile("a.py", "", frozenset(range(1, 30)))
        }
        item = {
            "severity": "P2",
            "path": "a.py",
            "line": 1,
            "title": "Repeated finding",
            "failure_scenario": "The same issue is returned more than once.",
            "remediation": "Publish it once.",
        }
        payload = {"findings": [item, dict(item)]}

        findings, rejected = review.validate_findings(payload, parsed, limit=20)

        self.assertEqual(len(findings), 1)
        self.assertEqual(rejected, 1)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class ApiClientTest(unittest.TestCase):
    def test_llm_client_uses_bearer_key_and_expected_model(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse(
                {"choices": [{"message": {"content": '{"findings": []}'}}]}
            )
        )
        client = review.LlmClient(
            "https://example.invalid/v3/chat/completions",
            "secret-value",
            "test-model",
            opener=opener,
            sleeper=mock.Mock(),
        )

        self.assertEqual(client.review("system", "diff"), {"findings": []})

        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
        self.assertEqual(opener.call_args.kwargs["timeout"], 90)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["max_tokens"], review.MAX_RESPONSE_TOKENS)
        self.assertGreaterEqual(review.MAX_RESPONSE_TOKENS, 8_000)

    def test_empty_content_is_flagged_incomplete(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse(
                {"choices": [{"message": {"content": ""}}]}
            )
        )
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, mock.Mock()
        )

        self.assertEqual(
            client.review("system", "diff"),
            {"findings": [], "incomplete": True},
        )

    def test_whitespace_content_is_flagged_incomplete(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse(
                {"choices": [{"message": {"content": "   \n\t "}}]}
            )
        )
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, mock.Mock()
        )

        self.assertEqual(
            client.review("system", "diff"),
            {"findings": [], "incomplete": True},
        )

    def test_missing_content_key_raises_model_response_error(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse({"choices": [{"message": {}}]})
        )
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, mock.Mock()
        )

        with self.assertRaises(review.ModelResponseError):
            client.review("system", "diff")

    def test_malformed_message_raises_model_response_error(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse({"choices": [{"message": "oops"}]})
        )
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, mock.Mock()
        )

        with self.assertRaises(review.ModelResponseError):
            client.review("system", "diff")

    def test_empty_content_ignores_reasoning_prose(self) -> None:
        opener = mock.Mock(
            return_value=FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": "Let me analyze this diff...",
                            }
                        }
                    ]
                }
            )
        )
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, mock.Mock()
        )

        self.assertEqual(
            client.review("system", "diff"),
            {"findings": [], "incomplete": True},
        )

    def test_llm_client_retries_429_twice(self) -> None:
        error = HTTPError("url", 429, "rate limited", {}, None)
        opener = mock.Mock(
            side_effect=[
                error,
                error,
                FakeResponse(
                    {"choices": [{"message": {"content": '{"findings": []}'}}]}
                ),
            ]
        )
        sleeper = mock.Mock()
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, sleeper
        )

        client.review("system", "diff")

        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleeper.call_args_list, [mock.call(1), mock.call(2)])
        error.close()

    def test_llm_client_does_not_retry_authentication_failure(self) -> None:
        error = HTTPError("url", 401, "unauthorized", {}, None)
        opener = mock.Mock(side_effect=error)
        sleeper = mock.Mock()
        client = review.LlmClient(
            "https://example.invalid", "key", "model", opener, sleeper
        )

        with self.assertRaises(HTTPError):
            client.review("system", "diff")

        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()
        error.close()

    def test_github_client_paginates_files(self) -> None:
        first_page = [
            {"filename": f"file-{index}.py", "patch": "@@ -0,0 +1 @@\n+x"}
            for index in range(100)
        ]
        opener = mock.Mock(
            side_effect=[
                FakeResponse(first_page),
                FakeResponse(
                    [{"filename": "last.py", "patch": "@@ -0,0 +1 @@\n+x"}]
                ),
            ]
        )
        client = review.GitHubClient("owner/repo", "token", opener=opener)

        files = client.list_files(7)

        self.assertEqual(len(files), 101)
        self.assertEqual(files[-1]["filename"], "last.py")
        self.assertEqual(opener.call_count, 2)

    def test_create_review_uses_comment_event_and_right_side_lines(self) -> None:
        opener = mock.Mock(return_value=FakeResponse({"id": 123}))
        client = review.GitHubClient("owner/repo", "token", opener=opener)
        finding = review.Finding("P1", "a.py", 8, "Bug", "Failure", "Fix")

        client.create_review(7, "abc123", "summary", [finding])

        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], "abc123")
        self.assertEqual(payload["comments"][0]["side"], "RIGHT")
        self.assertEqual(payload["comments"][0]["line"], 8)

    def test_create_review_neutralizes_model_generated_mentions(self) -> None:
        opener = mock.Mock(return_value=FakeResponse({"id": 123}))
        client = review.GitHubClient("owner/repo", "token", opener=opener)
        finding = review.Finding(
            "P2",
            "a.py",
            8,
            "Notify @security-team",
            "A crafted prompt could mention @all-maintainers.",
            "Keep @mentions inert.",
        )

        client.create_review(7, "abc123", "summary", [finding])

        request = opener.call_args.args[0]
        comment = json.loads(request.data)["comments"][0]["body"]
        self.assertNotIn("@security-team", comment)
        self.assertNotIn("@all-maintainers", comment)
        self.assertIn("@\u200bsecurity-team", comment)


class FakeGitHub:
    def __init__(self) -> None:
        self.pull = {
            "draft": False,
            "head": {"sha": "head-1", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}},
            "title": "Change worker cancellation",
            "body": "Keep child processes from leaking.",
        }
        self.files = [
            {
                "filename": "worker.py",
                "patch": "@@ -1 +1,2 @@\n keep\n+stop()",
            }
        ]
        self.reviews: list[dict[str, object]] = []
        self.created: list[tuple[object, ...]] = []

    def get_pull(self, _number: int) -> dict[str, object]:
        return self.pull

    def list_files(self, _number: int) -> list[dict[str, object]]:
        return self.files

    def list_reviews(self, _number: int) -> list[dict[str, object]]:
        return self.reviews

    def create_review(self, *args: object) -> dict[str, int]:
        self.created.append(args)
        return {"id": 1}


class FakeLlm:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def review(self, _prompt: str, chunk: str) -> dict[str, object]:
        self.inputs.append(chunk)
        return {
            "findings": [
                {
                    "severity": "P1",
                    "path": "worker.py",
                    "line": 2,
                    "title": "Cancellation is not forwarded",
                    "failure_scenario": "The worker survives the wrapper.",
                    "remediation": "Terminate the worker process group.",
                }
            ]
        }


class OrchestrationTest(unittest.TestCase):
    def test_collect_files_anchors_renames_to_the_new_path(self) -> None:
        files, skipped = review.collect_files(
            [
                {
                    "filename": "new_name.py",
                    "previous_filename": "old_name.py",
                    "status": "renamed",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        )

        self.assertEqual(skipped, [])
        self.assertEqual(files[0].path, "new_name.py")
        self.assertEqual(files[0].right_lines, frozenset({1}))

    def test_reviews_fork_pull_request(self) -> None:
        github = FakeGitHub()
        github.pull["head"]["repo"]["full_name"] = "fork/repo"

        result = review.run_review(github, FakeLlm(), 7, "prompt")

        self.assertEqual(result, "published")
        self.assertEqual(len(github.created), 1)

    def test_aborts_when_head_changes_before_publication(self) -> None:
        github = FakeGitHub()
        first = dict(github.pull)
        second = {**github.pull, "head": {**github.pull["head"], "sha": "head-2"}}
        github.get_pull = mock.Mock(side_effect=[first, second])

        result = review.run_review(github, FakeLlm(), 7, "prompt")

        self.assertEqual(result, "stale")
        self.assertEqual(github.created, [])

    def test_skips_existing_review_marker_for_same_sha(self) -> None:
        github = FakeGitHub()
        github.reviews = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- llm-pr-review:head-1 -->",
            }
        ]

        result = review.run_review(github, FakeLlm(), 7, "prompt")

        self.assertEqual(result, "duplicate")
        self.assertEqual(github.created, [])

    def test_existing_review_ignores_null_user(self) -> None:
        reviews = [{"user": None, "body": "<!-- llm-pr-review:head-1 -->"}]

        self.assertFalse(review.has_existing_review(reviews, "head-1"))

    def test_posts_validated_comment_review(self) -> None:
        github = FakeGitHub()

        result = review.run_review(github, FakeLlm(), 7, "prompt")

        self.assertEqual(result, "published")
        number, sha, body, findings = github.created[0]
        self.assertEqual((number, sha), (7, "head-1"))
        self.assertIn("<!-- llm-pr-review:head-1 -->", body)
        self.assertEqual(len(findings), 1)

    def test_no_findings_still_posts_sha_summary(self) -> None:
        github = FakeGitHub()
        llm = mock.Mock()
        llm.review.return_value = {"findings": []}

        review.run_review(github, llm, 7, "prompt")

        self.assertIn("no actionable findings", github.created[0][2])

    def test_model_input_contains_bounded_pr_context(self) -> None:
        github = FakeGitHub()
        llm = FakeLlm()

        review.run_review(github, llm, 7, "prompt")

        self.assertIn("PR TITLE: Change worker cancellation", llm.inputs[0])
        self.assertIn("PR DESCRIPTION: Keep child processes", llm.inputs[0])
        self.assertIn("UNTRUSTED DIFF", llm.inputs[0])

    def test_summary_caps_the_skipped_path_list(self) -> None:
        skipped = [(f"generated/{index}.map", "generated") for index in range(55)]

        summary = review.build_summary("head-1", [], 0, skipped, False, 0)

        self.assertIn("`generated/49.map`", summary)
        self.assertNotIn("`generated/50.map`", summary)
        self.assertIn("5 additional skipped file(s)", summary)

    def test_summary_reports_partial_when_a_chunk_is_incomplete(self) -> None:
        summary = review.build_summary("head-1", [], 0, [], False, 1)

        self.assertIn("Coverage: Partial", summary)
        self.assertIn("empty model response", summary)

    def test_summary_reports_complete_when_no_chunk_is_incomplete(self) -> None:
        summary = review.build_summary("head-1", [], 0, [], False, 0)

        self.assertIn("Coverage: Complete", summary)
        self.assertNotIn("empty model response", summary)

    def test_run_review_reports_partial_on_empty_model_response(self) -> None:
        github = FakeGitHub()
        llm = mock.Mock()
        llm.review.return_value = {"findings": [], "incomplete": True}

        review.run_review(github, llm, 7, "prompt")

        self.assertIn("Coverage: Partial", github.created[0][2])
        self.assertIn("empty model response", github.created[0][2])


class WorkflowContractTest(unittest.TestCase):
    def test_workflow_uses_trusted_event_and_allows_forks(self) -> None:
        workflow = SCRIPT_DIR.parent.joinpath("workflows/llm-pr-review.yml").read_text()

        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("head.repo.full_name == github.repository", workflow)
        self.assertIn("!github.event.pull_request.draft", workflow)
        self.assertNotIn("pull_request.head.sha", workflow)
        self.assertNotIn("pull_request.head.ref", workflow)

    def test_workflow_has_least_permissions_and_base_checkout(self) -> None:
        workflow = SCRIPT_DIR.parent.joinpath("workflows/llm-pr-review.yml").read_text()

        self.assertIn("contents: read", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("pull_request.base.sha", workflow)
        self.assertIn("persist-credentials: false", workflow)


if __name__ == "__main__":
    unittest.main()
