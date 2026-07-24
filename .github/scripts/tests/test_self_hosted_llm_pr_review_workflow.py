import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "self-hosted-llm-pr-review.yml"


class SelfHostedLlmPrReviewWorkflowTest(unittest.TestCase):
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

    def test_targets_self_hosted_runner_and_environment(self):
        self.assertIn("runs-on: [self-hosted, Linux, X64]", self.workflow)
        self.assertIn("environment: self-hosted-env", self.workflow)

    def test_maps_self_hosted_environment_configuration(self):
        expected = (
            "LLM_REVIEW_API_KEY: ${{ secrets.LLM_API_KEY }}",
            "LLM_REVIEW_BASE_URL: ${{ vars.LLM_BASE_URL }}",
            "LLM_REVIEW_MODEL: ${{ vars.LLM_MODEL }}",
            "LLM_REVIEW_ID: self-hosted-llm-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.workflow)

    def test_pins_checkout_and_uses_distinct_concurrency(self):
        self.assertIn(
            "group: self-hosted-llm-pr-review-${{ github.event.pull_request.number }}",
            self.workflow,
        )
        self.assertRegex(
            self.workflow,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )


if __name__ == "__main__":
    unittest.main()
