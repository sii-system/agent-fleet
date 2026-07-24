import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


class LlmPrReviewWorkflowReuseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hosted = WORKFLOWS.joinpath("llm-pr-review.yml").read_text(
            encoding="utf-8"
        )
        cls.self_hosted = WORKFLOWS.joinpath(
            "self-hosted-llm-pr-review.yml"
        ).read_text(encoding="utf-8")
        cls.reusable = WORKFLOWS.joinpath(
            "reusable-llm-pr-review.yml"
        ).read_text(encoding="utf-8")

    def test_reviewers_remain_separate_top_level_workflows(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn('"on":\n  pull_request_target:', workflow)
                self.assertIn(
                    "permissions:\n  contents: read\n  pull-requests: write",
                    workflow,
                )
                self.assertIn(
                    "uses: ./.github/workflows/reusable-llm-pr-review.yml",
                    workflow,
                )
                self.assertNotIn("actions/checkout@", workflow)
                self.assertNotIn("llm_pr_review.py", workflow)

    def test_hosted_skips_drafts_while_self_hosted_reviews_them(self):
        self.assertIn("!github.event.pull_request.draft", self.hosted)
        self.assertNotIn("!github.event.pull_request.draft", self.self_hosted)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review]",
            self.self_hosted,
        )

    def test_hosted_workflow_keeps_hosted_configuration(self):
        expected = (
            "name: LLM PR Review",
            "group: llm-pr-review-${{ github.event.pull_request.number }}",
            """runner_json: '["ubuntu-latest"]'""",
            "environment: llm-pr-review",
            "api_key_secret: LLM_REVIEW_API_KEY",
            "base_url_variable: LLM_REVIEW_BASE_URL",
            "model_variable: LLM_REVIEW_MODEL",
            "review_id: llm-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.hosted)

    def test_self_hosted_workflow_keeps_distinct_configuration(self):
        expected = (
            "name: Self-Hosted LLM PR Review",
            "group: self-hosted-llm-pr-review-"
            "${{ github.event.pull_request.number }}",
            """runner_json: '["self-hosted", "Linux", "X64"]'""",
            "environment: self-hosted-env",
            "api_key_secret: LLM_API_KEY",
            "base_url_variable: LLM_BASE_URL",
            "model_variable: LLM_MODEL",
            "review_id: self-hosted-llm-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)

    def test_reusable_workflow_owns_the_review_execution_policy(self):
        expected = (
            '"on":\n  workflow_call:',
            "runs-on: ${{ fromJSON(inputs.runner_json) }}",
            "environment: ${{ inputs.environment }}",
            "timeout-minutes: 15",
            "pull_request.base.sha",
            "persist-credentials: false",
            "LLM_REVIEW_API_KEY: ${{ secrets[inputs.api_key_secret] }}",
            "LLM_REVIEW_BASE_URL: ${{ vars[inputs.base_url_variable] }}",
            "LLM_REVIEW_MODEL: ${{ vars[inputs.model_variable] }}",
            "LLM_REVIEW_ID: ${{ inputs.review_id }}",
            "--prompt-path .github/scripts/llm_review_prompt.md",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.reusable)
        self.assertRegex(
            self.reusable,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )
        self.assertEqual(self.reusable.count("actions/checkout@"), 1)
        self.assertEqual(self.reusable.count("llm_pr_review.py"), 1)


if __name__ == "__main__":
    unittest.main()
