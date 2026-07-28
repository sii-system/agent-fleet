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
        scripts = ROOT / ".github" / "scripts"
        cls.pi_prompt = scripts.joinpath("pi_review_prompt.md").read_text(
            encoding="utf-8"
        )
        cls.llm_prompt = scripts.joinpath("llm_review_prompt.md").read_text(
            encoding="utf-8"
        )

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

    def test_hosted_uses_direct_diff_review_without_pi(self):
        self.assertIn("llm_pr_review.py", self.hosted)
        self.assertIn("--prompt-path .github/scripts/llm_review_prompt.md", self.hosted)
        self.assertIn("'llm-pr-review'", self.hosted)
        self.assertNotIn("pi_pr_review.py", self.hosted)
        self.assertNotIn("npm install", self.hosted)
        self.assertNotIn("PI_VERSION", self.hosted)

        self.assertIn("pi_pr_review.py", self.self_hosted)
        self.assertIn(
            "--prompt-path .github/scripts/pi_review_prompt.md",
            self.self_hosted,
        )

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
            (
                "group: llm-pr-review-"
                "${{ github.event_name }}-"
                "${{ github.event.pull_request.number || inputs.pr_number }}"
            ),
            "runs-on: ubuntu-latest",
            "environment: llm-pr-review",
            "'llm-pr-review'",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.hosted)

    def test_self_hosted_workflow_keeps_distinct_configuration(self):
        expected = (
            "name: Self-Hosted LLM PR Review",
            (
                "self-hosted-llm-pr-review-"
                "${{ github.event_name }}-"
                "${{ github.event.pull_request.number || inputs.pr_number }}"
            ),
            "runs-on: [self-hosted, Linux, X64]",
            "environment: self-hosted-env",
            "'self-hosted-pi-pr-review'",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)

    def test_workflows_label_their_distinct_review_tiers(self):
        self.assertIn("LLM_REVIEW_LABEL: Fast review — diff only", self.hosted)
        self.assertIn(
            "LLM_REVIEW_LABEL: Deep review — 3 lenses, codebase context",
            self.self_hosted,
        )

    def test_review_tiers_use_distinct_prompt_contracts(self):
        self.assertIn("{{LENS}}", self.pi_prompt)
        self.assertIn("trusted base checkout", self.pi_prompt)
        self.assertIn("line to null", self.pi_prompt)

        self.assertIn("diff-only review", self.llm_prompt)
        self.assertIn("Do not make cross-file claims", self.llm_prompt)
        self.assertIn("line to null", self.llm_prompt)

        for prompt in (self.pi_prompt, self.llm_prompt):
            self.assertNotIn(
                "Prefer no finding over a speculative finding",
                prompt,
            )

    def test_workflows_support_a_trusted_manual_pr_canary(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn("workflow_dispatch:", workflow)
                self.assertIn("pr_number:", workflow)
                self.assertIn("required: true", workflow)
                self.assertIn("name: Resolve review target", workflow)
                self.assertIn("inputs.pr_number", workflow)
                self.assertIn("api.github.com/repos", workflow)
                self.assertIn("ref: ${{ steps.target.outputs.base_sha }}", workflow)
                self.assertIn(
                    '--event-path "${{ steps.target.outputs.event_path }}"',
                    workflow,
                )
                self.assertIn("-canary", workflow)
                self.assertIn(
                    "${{ github.event_name }}-${{ github.event.pull_request.number",
                    workflow,
                )

    def test_workflows_keep_the_same_event_and_checkout_policy(self):
        expected = (
            "timeout-minutes: 20",
            "ref: ${{ steps.target.outputs.base_sha }}",
            "persist-credentials: false",
            "LLM_REVIEW_API_KEY: ${{ secrets.LLM_REVIEW_API_KEY }}",
            "LLM_REVIEW_BASE_URL: ${{ vars.LLM_REVIEW_BASE_URL }}",
            "LLM_REVIEW_MODEL: ${{ vars.LLM_REVIEW_MODEL }}",
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

    def test_environment_configuration_uses_shared_names(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn("secrets.LLM_REVIEW_API_KEY", workflow)
                self.assertNotIn("secrets[", workflow)
                self.assertNotIn("vars[", workflow)
                self.assertNotIn("api_key_secret", workflow)
                self.assertNotIn("base_url_variable", workflow)
                self.assertNotIn("model_variable", workflow)

    def test_self_hosted_requires_the_exact_pi_version(self):
        self.assertIn('PI_VERSION: "0.81.1"', self.self_hosted)
        self.assertIn(
            'if [[ "$INSTALLED_VERSION" != "$PI_VERSION" ]]',
            self.self_hosted,
        )
        self.assertIn("Expected pi version", self.self_hosted)
        self.assertNotIn("PI_VERSION", self.hosted)

    def test_reusable_workflow_is_removed(self):
        self.assertFalse(
            WORKFLOWS.joinpath("reusable-llm-pr-review.yml").exists()
        )


if __name__ == "__main__":
    unittest.main()
