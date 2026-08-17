from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import pi_pr_summary as summary
except ModuleNotFoundError:
    summary = None


class SummaryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(summary, "pi_pr_summary.py must exist")

    def test_validates_three_part_summary(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Adds one-time PR summaries."],
                "diagram": "sequenceDiagram\n  PR->>Pi: summarize",
                "assessment": "The change is isolated to the hosted workflow.",
            }
        )

        self.assertEqual(result.description, ("Adds one-time PR summaries.",))
        self.assertEqual(
            result.diagram,
            "sequenceDiagram\n  PR->>Pi: summarize",
        )
        self.assertEqual(
            result.assessment,
            "The change is isolated to the hosted workflow.",
        )

    def test_rejects_missing_description(self) -> None:
        with self.assertRaisesRegex(summary.PiSummaryError, "description"):
            summary.validate_summary(
                {
                    "description": [],
                    "diagram": None,
                    "assessment": "Assessment.",
                }
            )

    def test_accepts_ascii_diagram_fallback(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Summarizes the pull request."],
                "diagram": "Controller\n    |\n    v\nWorker",
                "assessment": "The change keeps the existing architecture.",
            }
        )

        self.assertEqual(result.diagram, "Controller\n    |\n    v\nWorker")

    def test_rejects_fenced_ascii_diagram(self) -> None:
        with self.assertRaisesRegex(summary.PiSummaryError, "diagram"):
            summary.validate_summary(
                {
                    "description": ["Summary."],
                    "diagram": "Controller\n```\n</details>",
                    "assessment": "Assessment.",
                }
            )

    def test_rejects_unsafe_mermaid(self) -> None:
        with self.assertRaisesRegex(summary.PiSummaryError, "diagram"):
            summary.validate_summary(
                {
                    "description": ["Summary."],
                    "diagram": "flowchart TD\n  click A https://example.com",
                    "assessment": "Assessment.",
                }
            )

    def test_rejects_mermaid_image_node(self) -> None:
        with self.assertRaisesRegex(summary.PiSummaryError, "image node"):
            summary.validate_summary(
                {
                    "description": ["Summary."],
                    "diagram": (
                        'flowchart TD\n  A@{ img: "https://attacker.example/pixel" }'
                    ),
                    "assessment": "Assessment.",
                }
            )

    def test_quotes_generated_flowchart_labels(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Adds a manual canary."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[pull_request_target] --> R{Resolve review target}\n"
                    "  R --> API[GET /repos/{owner}/{repo}/pulls/{n}]"
                ),
                "assessment": "The canary reuses the existing review path.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["pull_request_target"] --> R{"Resolve review target"}\n'
            '  R --> API["GET /repos/{owner}/{repo}/pulls/{n}"]',
        )

    def test_preserves_delimiters_inside_quoted_node_text(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes an indexed lookup."],
                "diagram": 'flowchart TD\n  A["Read items[0]"] --> B["Done"]',
                "assessment": "The flow is direct.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  A["Read items[0]"] --> B["Done"]',
        )

    def test_quotes_node_text_for_numeric_ids(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes an API lookup."],
                "diagram": "flowchart TD\n  1[GET /repos/{owner}/{repo}]",
                "assessment": "The flow is direct.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  1["GET /repos/{owner}/{repo}"]',
        )

    def test_does_not_rewrite_edge_text(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes an edge lookup."],
                "diagram": (
                    'flowchart TD\n  A["Start"] -->|"lookup key[x]"| B["Done"]'
                ),
                "assessment": "The flow is direct.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  A["Start"] -->|"lookup key[x]"| B["Done"]',
        )

    def test_quotes_rectangular_text_with_braces_once(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a configuration lookup."],
                "diagram": "flowchart TD\n  A[config{mode}]",
                "assessment": "The flow is direct.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  A["config{mode}"]',
        )

    def test_quotes_nodes_after_multi_node_connectors(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes parallel targets."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] --> B[One] & C[GET /repos/{owner}]"
                ),
                "assessment": "The flow fans out.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] --> B["One"] & C["GET /repos/{owner}"]',
        )

    def test_quotes_targets_after_circle_and_cross_links(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes alternate outcomes."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] --x B[GET /repos/{owner}]\n"
                    "  A --o C[GET /repos/{repo}]"
                ),
                "assessment": "The flow has two terminal outcomes.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] --x B["GET /repos/{owner}"]\n'
            '  A --o C["GET /repos/{repo}"]',
        )

    def test_ignores_node_syntax_in_mermaid_comments(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes the active flow."],
                "diagram": (
                    "flowchart TD\n"
                    "  %% old syntax: A --> C[unfinished\n"
                    "  A[Start] --> B[Done]"
                ),
                "assessment": "The commented syntax is inactive.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            "  %% old syntax: A --> C[unfinished\n"
            '  A["Start"] --> B["Done"]',
        )

    def test_quotes_rectangular_text_starting_with_a_brace(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a route parameter."],
                "diagram": "flowchart TD\n  A[{owner}]",
                "assessment": "The flow contains one route parameter.",
            }
        )

        self.assertEqual(result.diagram, 'flowchart TD\n  A["{owner}"]')

    def test_quotes_targets_after_dotted_links(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes dotted transitions."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] -. yes .-> B[GET /repos/{owner}]\n"
                    "  B -.- C[GET /repos/{repo}]"
                ),
                "assessment": "The flow uses two dotted link forms.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] -. yes .-> B["GET /repos/{owner}"]\n'
            '  B -.- C["GET /repos/{repo}"]',
        )

    def test_quotes_targets_after_lengthened_dotted_open_links(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a lengthened dotted transition."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] -..- B[GET /repos/{owner}]"
                ),
                "assessment": "The flow uses one lengthened dotted link.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] -..- B["GET /repos/{owner}"]',
        )

    def test_does_not_treat_pipes_inside_node_labels_as_edge_text(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes piped input."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Input | output] --> B[GET /repos/{owner}]"
                ),
                "assessment": "The flow has two nodes.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Input | output"] --> B["GET /repos/{owner}"]',
        )

    def test_quotes_nodes_after_statement_separators(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes compact Mermaid."],
                "diagram": (
                    "flowchart TD; A[GET /repos/{owner}]; "
                    "B[GET /repos/{repo}]"
                ),
                "assessment": "The flow uses compact statements.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD; A[\"GET /repos/{owner}\"]; "
            'B["GET /repos/{repo}"]',
        )

    def test_preserves_multiline_quoted_node_labels(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes multiline text."],
                "diagram": (
                    'flowchart TD\n  A["`First line\nSecond line`"] --> B[Done]'
                ),
                "assessment": "The first node has a Markdown label.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  A["`First line\nSecond line`"] --> B["Done"]',
        )

    def test_quotes_targets_after_thick_open_links(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes thick transitions."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] === B[GET /repos/{owner}]\n"
                    "  B == yes === C[GET /repos/{repo}]"
                ),
                "assessment": "The flow uses two thick open links.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] === B["GET /repos/{owner}"]\n'
            '  B == yes === C["GET /repos/{repo}"]',
        )

    def test_quotes_explicit_subgraph_titles(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes an API subgraph."],
                "diagram": (
                    "flowchart TD\n"
                    "  subgraph API [GET /repos/{owner}]\n"
                    "    A[Start]\n"
                    "  end"
                ),
                "assessment": "The flow groups one API node.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  subgraph API ["GET /repos/{owner}"]\n'
            '    A["Start"]\n'
            "  end",
        )

    def test_quotes_targets_after_spaced_edge_labels(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a labeled transition."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Start] --> |yes| B[GET /repos/{owner}]"
                ),
                "assessment": "The flow has one labeled transition.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Start"] --> |yes| B["GET /repos/{owner}"]',
        )

    def test_ignores_unmatched_punctuation_inside_node_labels(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes raw input."],
                "diagram": (
                    "flowchart TD\n"
                    "  A[Input (raw] --> B[GET /repos/{owner}]"
                ),
                "assessment": "The flow has two nodes.",
            }
        )

        self.assertEqual(
            result.diagram,
            "flowchart TD\n"
            '  A["Input (raw"] --> B["GET /repos/{owner}"]',
        )

    def test_encodes_quotes_inside_quoted_node_labels(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a quoted status."],
                "diagram": 'flowchart TD\n  A["Return "ready" status"]',
                "assessment": "The flow has one status node.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  A["Return #quot;ready#quot; status"]',
        )

    def test_quotes_balanced_braces_inside_diamond_text(self) -> None:
        result = summary.validate_summary(
            {
                "description": ["Describes a configuration decision."],
                "diagram": "flowchart TD\n  R{Check config{mode}}",
                "assessment": "The flow branches on configuration.",
            }
        )

        self.assertEqual(
            result.diagram,
            'flowchart TD\n  R{"Check config{mode}"}',
        )


class SummaryRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(summary, "pi_pr_summary.py must exist")

    def test_renders_qodo_style_sections_and_neutralizes_mentions(self) -> None:
        value = summary.Summary(
            description=("Notify @maintainers after <deployment>.",),
            diagram="flowchart TD\n  PR --> Pi",
            assessment="Small, isolated change.",
        )

        body = summary.render_summary("Add <summary>", value)

        self.assertIn("<h3>PR Summary by Pi</h3>", body)
        self.assertIn("Add &lt;summary&gt;", body)
        self.assertIn("<summary>AI Description</summary>", body)
        self.assertIn("<summary>Diagram</summary>", body)
        self.assertIn("```mermaid\nflowchart TD\n  PR --> Pi\n```", body)
        self.assertIn("<summary>High-Level Assessment</summary>", body)
        self.assertIn("@\u200bmaintainers", body)
        self.assertIn("&lt;deployment&gt;", body)
        self.assertLess(
            body.index("<summary>High-Level Assessment</summary>"),
            body.index("<summary>AI Description</summary>"),
        )
        self.assertLess(
            body.index("<summary>AI Description</summary>"),
            body.index("<summary>Diagram</summary>"),
        )

    def test_omits_empty_diagram_section(self) -> None:
        value = summary.Summary(
            description=("Updates documentation.",),
            diagram=None,
            assessment="No component interaction needs a diagram.",
        )

        body = summary.render_summary("Update docs", value)

        self.assertNotIn("<summary>Diagram</summary>", body)

    def test_renders_ascii_diagram_as_plain_text(self) -> None:
        value = summary.Summary(
            description=("Uses an ASCII fallback.",),
            diagram="Controller\n    |\n    v\nWorker",
            assessment="The interaction remains visible without Mermaid.",
        )

        body = summary.render_summary("Update workflow", value)

        self.assertIn(
            "```text\nController\n    |\n    v\nWorker\n```",
            body,
        )
        self.assertNotIn("```mermaid", body)

    def test_escapes_markdown_links_in_untrusted_prose(self) -> None:
        value = summary.Summary(
            description=("**Deploy now**",),
            diagram=None,
            assessment="![status](https://attacker.example/pixel)",
        )

        body = summary.render_summary(
            "[Release report](https://attacker.example)",
            value,
        )

        self.assertIn(
            r"\[Release report\]\(https\:\/\/attacker\.example\)",
            body,
        )
        self.assertIn(r"\*\*Deploy now\*\*", body)
        self.assertIn(
            r"\!\[status\]\(https\:\/\/attacker\.example\/pixel\)",
            body,
        )
        self.assertNotIn("[Release report](https://attacker.example)", body)


class FakeGitHub:
    def __init__(self) -> None:
        self.pull = {
            "title": "Add one-time PR summary",
            "body": "Summarize the initial pull request.",
        }
        self.files = [
            {
                "filename": "src/summary.py",
                "status": "modified",
                "additions": 12,
                "deletions": 3,
                "patch": "@@ -1 +1,2 @@\n keep\n+summarize()",
            }
        ]
        self.comments: list[tuple[int, str]] = []

    def get_pull(self, _number: int) -> dict[str, object]:
        return self.pull

    def list_files(self, _number: int) -> list[dict[str, object]]:
        return self.files

    def create_issue_comment(self, number: int, body: str) -> dict[str, int]:
        self.comments.append((number, body))
        return {"id": 1}


class FakePi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def review(self, prompt: str, model_input: str) -> dict[str, object]:
        self.calls.append((prompt, model_input))
        return {
            "description": ["Adds an initial PR summary."],
            "diagram": "flowchart TD\n  PR --> Pi --> Comment",
            "assessment": "The implementation is isolated to PR automation.",
        }


class SummaryOrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(summary, "pi_pr_summary.py must exist")

    def test_calls_pi_once_and_posts_one_standalone_comment(self) -> None:
        github = FakeGitHub()
        pi = FakePi()
        self.assertTrue(hasattr(summary, "run_summary"))

        result = summary.run_summary(github, pi, 17, "summary prompt")

        self.assertEqual(result, "published")
        self.assertEqual(len(pi.calls), 1)
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(github.comments[0][0], 17)
        self.assertIn("<h3>PR Summary by Pi</h3>", github.comments[0][1])
        model_input = pi.calls[0][1]
        self.assertIn("PR TITLE: Add one-time PR summary", model_input)
        self.assertIn("src/summary.py (modified, +12/-3)", model_input)
        self.assertIn("UNTRUSTED DIFF:", model_input)

    def test_large_diff_is_bounded_without_extra_pi_calls(self) -> None:
        github = FakeGitHub()
        github.files = [
            {
                "filename": "src/large.py",
                "status": "modified",
                "additions": 2,
                "deletions": 0,
                "patch": "@@ -1 +1,2 @@\n " + ("x" * 29_000) + "\n+" + ("y" * 29_000),
            }
        ]
        pi = FakePi()
        self.assertTrue(hasattr(summary, "run_summary"))

        summary.run_summary(github, pi, 17, "summary prompt")

        self.assertEqual(len(pi.calls), 1)
        self.assertIn("DIFF TRUNCATED: yes", pi.calls[0][1])
        self.assertLess(len(pi.calls[0][1]), 60_000)

    def test_uses_every_chunk_returned_within_the_single_call_budget(self) -> None:
        github = FakeGitHub()
        pi = FakePi()
        with mock.patch.object(
            summary._review,
            "build_chunks",
            return_value=(["context only", "\n+ADDED_SENTINEL"], False),
        ):
            summary.run_summary(github, pi, 17, "summary prompt")

        self.assertEqual(len(pi.calls), 1)
        self.assertIn("ADDED_SENTINEL", pi.calls[0][1])

    def test_long_file_inventory_cannot_displace_diff(self) -> None:
        github = FakeGitHub()
        github.files = []
        for index in range(100):
            path = ("nested/" * 140) + f"file-{index}.py"
            github.files.append(
                {
                    "filename": path,
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": (
                        "@@ -0,0 +1 @@\n+DIFF_SENTINEL"
                        if index == 0
                        else None
                    ),
                }
            )
        pi = FakePi()

        summary.run_summary(github, pi, 17, "summary prompt")

        model_input = pi.calls[0][1]
        self.assertIn("DIFF TRUNCATED: yes", model_input)
        self.assertIn("UNTRUSTED DIFF:", model_input)
        self.assertIn("DIFF_SENTINEL", model_input)
        self.assertLessEqual(
            len(model_input.encode("utf-8")),
            summary.MAX_SUMMARY_INPUT_BYTES,
        )

    def test_multibyte_input_stays_within_single_argument_budget(self) -> None:
        self.assertTrue(hasattr(summary, "MAX_SUMMARY_INPUT_BYTES"))
        github = FakeGitHub()
        github.files = [
            {
                "filename": "src/unicode.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "@@ -0,0 +1 @@\n+" + ("测" * 30_000),
            }
        ]
        pi = FakePi()

        summary.run_summary(github, pi, 17, "summary prompt")

        self.assertEqual(len(pi.calls), 1)
        model_input = pi.calls[0][1]
        self.assertLessEqual(
            len(model_input.encode("utf-8")),
            summary.MAX_SUMMARY_INPUT_BYTES,
        )
        self.assertIn("DIFF TRUNCATED: yes", model_input)


class SummaryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(summary, "pi_pr_summary.py must exist")

    def test_main_reads_event_and_publishes_summary(self) -> None:
        github = FakeGitHub()
        pi = FakePi()
        self.assertTrue(hasattr(summary, "main"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            prompt_path = root / "prompt.md"
            event_path.write_text(
                json.dumps({"pull_request": {"number": 17}}),
                encoding="utf-8",
            )
            prompt_path.write_text("summary prompt", encoding="utf-8")
            argv = [
                "pi_pr_summary.py",
                "--event-path",
                str(event_path),
                "--prompt-path",
                str(prompt_path),
            ]
            environment = {
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_TOKEN": "token",
                "GITHUB_WORKSPACE": "/workspace",
                "LLM_REVIEW_BASE_URL": "https://api.example.com/v1",
                "LLM_REVIEW_API_KEY": "model-token",
                "LLM_REVIEW_MODEL": "model",
            }
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict("os.environ", environment, clear=True),
                mock.patch.object(
                    summary._review,
                    "GitHubClient",
                    return_value=github,
                ),
                mock.patch.object(
                    summary,
                    "PiClient",
                    return_value=pi,
                ) as pi_constructor,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = summary.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(pi.calls), 1)
        self.assertEqual(len(github.comments), 1)
        constructor_args = pi_constructor.call_args.kwargs
        self.assertEqual(
            constructor_args["repository_root"],
            Path("/workspace"),
        )


if __name__ == "__main__":
    unittest.main()
