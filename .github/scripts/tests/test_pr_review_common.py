from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import pr_review_common as review


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

    def test_validate_findings_retains_non_right_lines_and_sorts_severity(
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
                    "title": "Summary anchor",
                    "failure_scenario": "This is not an added line.",
                    "remediation": "Publish this finding in the summary.",
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

        self.assertEqual(
            [item.severity for item in findings],
            ["P0", "P1", "P2"],
        )
        self.assertEqual(rejected, 0)

    def test_validation_deduplicates_findings(self) -> None:
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

        findings, rejected = review.validate_findings(payload, parsed)

        self.assertEqual(len(findings), 1)
        self.assertEqual(rejected, 1)

    def test_route_findings_separates_inline_and_summary(self) -> None:
        files = {
            "a.py": review.ParsedFile("a.py", "", frozenset({8})),
        }
        findings = [
            review.Finding("P1", "a.py", 8, "Inline", "Failure", "Fix"),
            review.Finding("P2", "a.py", 7, "Context", "Failure", "Fix"),
            review.Finding("P3", "a.py", 8, "Minor", "Failure", "Fix"),
            review.Finding("P1", "helper.py", None, "Other", "Failure", "Fix"),
        ]

        inline, summary = review.route_findings(findings, files)

        self.assertEqual([item.title for item in inline], ["Inline"])
        self.assertEqual(
            [item.title for item in summary],
            ["Context", "Minor", "Other"],
        )

    def test_validation_retains_valid_unanchorable_findings(self) -> None:
        files = {
            "a.py": review.ParsedFile("a.py", "", frozenset({8})),
        }
        payload = {
            "findings": [
                {
                    "severity": "P2",
                    "path": "a.py",
                    "line": 7,
                    "title": "Context finding",
                    "failure_scenario": "The failure is not on an added line.",
                    "remediation": "Fix the surrounding logic.",
                },
                {
                    "severity": "P1",
                    "path": "helper.py",
                    "line": None,
                    "title": "Cross-file finding",
                    "failure_scenario": "The changed caller breaks this helper.",
                    "remediation": "Keep the helper contract compatible.",
                },
            ]
        }

        findings, rejected = review.validate_findings(payload, files)

        self.assertEqual(
            [item.title for item in findings],
            ["Cross-file finding", "Context finding"],
        )
        self.assertEqual(rejected, 0)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:  # noqa: PYI034 - keep Python 3.10 compatibility
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class GitHubClientTest(unittest.TestCase):
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

    def test_create_review_includes_lens_attribution(self) -> None:
        opener = mock.Mock(return_value=FakeResponse({"id": 123}))
        client = review.GitHubClient("owner/repo", "token", opener=opener)
        finding = review.Finding(
            "P1",
            "a.py",
            8,
            "Bug",
            "Failure",
            "Fix",
            ("correctness", "security"),
        )

        client.create_review(7, "abc123", "summary", [finding])

        request = opener.call_args.args[0]
        comment = json.loads(request.data)["comments"][0]["body"]
        self.assertIn("Flagged by: correctness + security", comment)

    def test_create_issue_comment_posts_standalone_body(self) -> None:
        opener = mock.Mock(return_value=FakeResponse({"id": 123}))
        client = review.GitHubClient("owner/repo", "token", opener=opener)
        self.assertTrue(hasattr(client, "create_issue_comment"))

        client.create_issue_comment(7, "PR summary")

        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/issues/7/comments"))
        self.assertEqual(json.loads(request.data), {"body": "PR summary"})


class ReviewCommonTest(unittest.TestCase):
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

    def test_existing_review_ignores_null_user(self) -> None:
        reviews = [{"user": None, "body": "<!-- pi-pr-review:head-1 -->"}]

        self.assertFalse(review.has_existing_review(reviews, "head-1"))

    def test_model_input_contains_bounded_pr_context(self) -> None:
        model_input = review.build_model_input(
            {
                "title": "Change worker cancellation",
                "body": "Keep child processes from leaking.",
            },
            "FILE worker.py\n+ RIGHT 2: stop()",
        )

        self.assertIn("PR TITLE: Change worker cancellation", model_input)
        self.assertIn("PR DESCRIPTION: Keep child processes", model_input)
        self.assertIn("UNTRUSTED DIFF", model_input)

    def test_summary_caps_the_skipped_path_list(self) -> None:
        skipped = [(f"generated/{index}.map", "generated") for index in range(55)]

        summary = review.build_summary("head-1", [], 0, skipped, False, 0)

        self.assertIn("`generated/49.map`", summary)
        self.assertNotIn("`generated/50.map`", summary)
        self.assertIn("5 additional skipped file(s)", summary)

    def test_summary_reports_partial_when_a_lens_is_incomplete(self) -> None:
        summary = review.build_summary(
            "head-1",
            [],
            0,
            [],
            False,
            incomplete_lenses=1,
        )

        self.assertIn("Coverage: Partial", summary)
        self.assertIn("empty model response", summary)

    def test_summary_reports_complete_when_no_lens_is_incomplete(self) -> None:
        summary = review.build_summary("head-1", [], 0, [], False)

        self.assertIn("Coverage: Complete", summary)
        self.assertNotIn("empty model response", summary)

if __name__ == "__main__":
    unittest.main()
