import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


class PiPrReviewWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.self_hosted = WORKFLOWS.joinpath(
            "self-hosted-llm-pr-review.yml"
        ).read_text(encoding="utf-8")

    def test_duplicate_hosted_workflow_is_removed(self):
        self.assertFalse(WORKFLOWS.joinpath("llm-pr-review.yml").exists())

    def test_self_hosted_reviewer_is_the_only_review_entrypoint(self):
        self.assertIn('"on":\n  pull_request_target:', self.self_hosted)
        self.assertIn(
            "permissions:\n  contents: read\n  pull-requests: write",
            self.self_hosted,
        )
        self.assertNotIn("reusable-llm-pr-review.yml", self.self_hosted)
        self.assertNotIn("secrets: inherit", self.self_hosted)
        self.assertIn("actions/checkout@", self.self_hosted)
        self.assertIn("pi_pr_review.py", self.self_hosted)

    def test_self_hosted_reviews_drafts_on_supported_events(self):
        self.assertNotIn("!github.event.pull_request.draft", self.self_hosted)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review]",
            self.self_hosted,
        )

    def test_self_hosted_workflow_keeps_distinct_configuration(self):
        expected = (
            "name: Self-Hosted LLM PR Review",
            (
                "group: self-hosted-llm-pr-review-"
                "${{ github.event.pull_request.number }}"
            ),
            "runs-on: [self-hosted, Linux, X64, container, code-review]",
            "environment: self-hosted-env",
            "LLM_REVIEW_ID: self-hosted-pi-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)

    def test_self_hosted_keeps_the_review_execution_policy(self):
        expected = (
            "timeout-minutes: 20",
            "pull_request.base.sha",
            "persist-credentials: false",
            "LLM_REVIEW_API_KEY: ${{ secrets.LLM_REVIEW_API_KEY }}",
            "LLM_REVIEW_BASE_URL: ${{ vars.LLM_REVIEW_BASE_URL }}",
            "LLM_REVIEW_MODEL: ${{ vars.LLM_REVIEW_MODEL }}",
            "--prompt-path .github/scripts/pi_review_prompt.md",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)
        self.assertRegex(
            self.self_hosted,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )
        self.assertEqual(self.self_hosted.count("actions/checkout@"), 1)
        self.assertEqual(self.self_hosted.count("pi_pr_review.py"), 1)

    def test_environment_configuration_uses_shared_names(self):
        self.assertIn("secrets.LLM_REVIEW_API_KEY", self.self_hosted)
        self.assertNotIn("secrets[", self.self_hosted)
        self.assertNotIn("vars[", self.self_hosted)
        self.assertNotIn("api_key_secret", self.self_hosted)
        self.assertNotIn("base_url_variable", self.self_hosted)
        self.assertNotIn("model_variable", self.self_hosted)

    def test_self_hosted_requires_the_exact_pi_version(self):
        self.assertIn('PI_VERSION: "0.81.1"', self.self_hosted)
        self.assertIn(
            'if [[ "$INSTALLED_VERSION" != "$PI_VERSION" ]]',
            self.self_hosted,
        )
        self.assertIn("Expected pi version", self.self_hosted)

    def test_reusable_workflow_is_removed(self):
        self.assertFalse(
            WORKFLOWS.joinpath("reusable-llm-pr-review.yml").exists()
        )


class PiPrSummaryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = WORKFLOWS / "pi-pr-summary.yml"

    def _workflow(self) -> str:
        self.assertTrue(self.path.exists(), "pi-pr-summary.yml must exist")
        return self.path.read_text(encoding="utf-8")

    def test_runs_once_for_draft_and_fork_pr_open_on_main(self) -> None:
        workflow = self._workflow()

        self.assertIn(
            '"on":\n  pull_request_target:\n'
            "    types: [opened]\n    branches: [main]",
            workflow,
        )
        self.assertNotIn("github.event.pull_request.draft", workflow)
        self.assertNotIn("github.event.pull_request.head.repo.full_name", workflow)
        self.assertNotIn("reopened", workflow)
        self.assertNotIn("synchronize", workflow)
        self.assertNotIn("ready_for_review", workflow)

    def test_uses_self_hosted_trusted_base_and_shared_model_configuration(
        self,
    ) -> None:
        workflow = self._workflow()
        expected = (
            "name: Pi PR Summary",
            "contents: read",
            "pull-requests: write",
            "runs-on: [self-hosted, Linux, X64]",
            "environment: self-hosted-env",
            "pull_request.base.sha",
            "persist-credentials: false",
            'PI_VERSION: "0.81.1"',
            "Verify pi is available",
            "secrets.LLM_REVIEW_API_KEY",
            "vars.LLM_REVIEW_BASE_URL",
            "vars.LLM_REVIEW_MODEL",
            "python3 .github/scripts/pi_pr_summary.py",
            "--prompt-path .github/scripts/pi_summary_prompt.md",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, workflow)
        self.assertEqual(workflow.count("pi_pr_summary.py"), 1)
        self.assertRegex(
            workflow,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )
        self.assertNotIn("npm install", workflow)

    def test_prompt_requires_only_the_three_summary_sections(self) -> None:
        prompt_path = ROOT / ".github" / "scripts" / "pi_summary_prompt.md"
        self.assertTrue(prompt_path.exists(), "pi_summary_prompt.md must exist")
        prompt = prompt_path.read_text(encoding="utf-8")

        self.assertIn("untrusted data", prompt)
        self.assertIn('"description"', prompt)
        self.assertIn('"diagram"', prompt)
        self.assertIn('"assessment"', prompt)
        self.assertIn("trusted base checkout", prompt)
        self.assertNotIn('"findings"', prompt)
        self.assertNotIn("labels", prompt.casefold())

    def test_prompt_keeps_mermaid_node_quotes_out_of_raw_json(self) -> None:
        prompt = (ROOT / ".github" / "scripts" / "pi_summary_prompt.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("adds double quotes after parsing", prompt)
        self.assertIn("escape line breaks as `\\n`", prompt)
        self.assertIn("`A[Start] --> B{Ready?}`", prompt)
        self.assertNotIn('A["Start"]', prompt)


if __name__ == "__main__":
    unittest.main()
