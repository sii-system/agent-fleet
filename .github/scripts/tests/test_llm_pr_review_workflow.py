import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "llm-pr-review.yml"


class LlmPrReviewWorkflowMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_uses_the_existing_review_security_policy(self):
        self.assertIn('"on":\n  pull_request_target:', self.workflow)
        self.assertIn("!github.event.pull_request.draft", self.workflow)
        self.assertIn("permissions:\n  contents: read\n  pull-requests: write", self.workflow)
        self.assertIn("pull_request.base.sha", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertNotIn("pull_request.head.sha", self.workflow)
        self.assertNotIn("pull_request.head.ref", self.workflow)

    def test_one_matrix_drives_both_reviewers(self):
        expected = (
            "strategy:",
            "fail-fast: false",
            "runner: ubuntu-latest",
            "runner: [self-hosted, Linux, X64]",
            "environment: llm-pr-review",
            "environment: self-hosted-env",
            "review_id: llm-pr-review",
            "review_id: self-hosted-llm-pr-review",
            "runs-on: ${{ matrix.runner }}",
            "environment: ${{ matrix.environment }}",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.workflow)
        self.assertEqual(self.workflow.count("actions/checkout@"), 1)
        self.assertEqual(self.workflow.count("llm_pr_review.py"), 1)

    def test_maps_each_environment_configuration(self):
        expected = (
            "api_key_secret: LLM_REVIEW_API_KEY",
            "api_key_secret: LLM_API_KEY",
            "base_url_variable: LLM_REVIEW_BASE_URL",
            "base_url_variable: LLM_BASE_URL",
            "model_variable: LLM_REVIEW_MODEL",
            "model_variable: LLM_MODEL",
            "LLM_REVIEW_API_KEY: ${{ secrets[matrix.api_key_secret] }}",
            "LLM_REVIEW_BASE_URL: ${{ vars[matrix.base_url_variable] }}",
            "LLM_REVIEW_MODEL: ${{ vars[matrix.model_variable] }}",
            "LLM_REVIEW_ID: ${{ matrix.review_id }}",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.workflow)

    def test_pins_checkout_and_uses_shared_concurrency(self):
        self.assertIn(
            "group: llm-pr-review-${{ github.event.pull_request.number }}",
            self.workflow,
        )
        self.assertRegex(
            self.workflow,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )


if __name__ == "__main__":
    unittest.main()
