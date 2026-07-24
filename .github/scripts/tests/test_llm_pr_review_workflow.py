import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


class LlmPrReviewWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hosted = WORKFLOWS.joinpath("llm-pr-review.yml").read_text(
            encoding="utf-8"
        )
        cls.self_hosted = WORKFLOWS.joinpath(
            "self-hosted-llm-pr-review.yml"
        ).read_text(encoding="utf-8")

    def test_reviewers_remain_separate_top_level_workflows(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn('"on":\n  pull_request_target:', workflow)
                self.assertIn(
                    "permissions:\n  contents: read\n  pull-requests: write",
                    workflow,
                )
                self.assertNotIn("reusable-llm-pr-review.yml", workflow)
                self.assertNotIn("secrets: inherit", workflow)
                self.assertIn("actions/checkout@", workflow)
                self.assertIn("llm_pr_review.py", workflow)

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
            "runs-on: ubuntu-latest",
            "environment: llm-pr-review",
            "LLM_REVIEW_ID: llm-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.hosted)

    def test_self_hosted_workflow_keeps_distinct_configuration(self):
        expected = (
            "name: Self-Hosted LLM PR Review",
            "group: self-hosted-llm-pr-review-"
            "${{ github.event.pull_request.number }}",
            "runs-on: [self-hosted, Linux, X64]",
            "environment: self-hosted-env",
            "LLM_REVIEW_ID: self-hosted-llm-pr-review",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)

    def test_workflows_keep_the_same_review_execution_policy(self):
        expected = (
            "timeout-minutes: 20",
            "pull_request.base.sha",
            "persist-credentials: false",
            "LLM_REVIEW_API_KEY: ${{ secrets.LLM_REVIEW_API_KEY }}",
            "LLM_REVIEW_BASE_URL: ${{ vars.LLM_REVIEW_BASE_URL }}",
            "LLM_REVIEW_MODEL: ${{ vars.LLM_REVIEW_MODEL }}",
            "--prompt-path .github/scripts/llm_review_prompt.md",
        )
        for workflow in (self.hosted, self.self_hosted):
            for setting in expected:
                with self.subTest(
                    workflow=workflow.splitlines()[0],
                    setting=setting,
                ):
                    self.assertIn(setting, workflow)
            self.assertRegex(
                workflow,
                re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
            )
            self.assertEqual(workflow.count("actions/checkout@"), 1)
            self.assertEqual(workflow.count("llm_pr_review.py"), 1)

    def test_environment_configuration_uses_shared_names(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn("secrets.LLM_REVIEW_API_KEY", workflow)
                self.assertNotIn("secrets[", workflow)
                self.assertNotIn("vars[", workflow)
                self.assertNotIn("api_key_secret", workflow)
                self.assertNotIn("base_url_variable", workflow)
                self.assertNotIn("model_variable", workflow)

    def test_reusable_workflow_is_removed(self):
        self.assertFalse(
            WORKFLOWS.joinpath("reusable-llm-pr-review.yml").exists()
        )


if __name__ == "__main__":
    unittest.main()
