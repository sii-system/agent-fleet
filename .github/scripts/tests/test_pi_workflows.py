import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / ".github" / "scripts"


class PiReviewArchitectureTest(unittest.TestCase):
    def test_only_pi_review_and_summary_workflows_remain(self) -> None:
        self.assertFalse(WORKFLOWS.joinpath("llm-pr-review.yml").exists())
        self.assertTrue(
            WORKFLOWS.joinpath("self-hosted-llm-pr-review.yml").exists()
        )
        self.assertTrue(WORKFLOWS.joinpath("pi-pr-summary.yml").exists())

    def test_pi_entrypoints_use_a_non_executable_shared_module(self) -> None:
        common_path = SCRIPTS.joinpath("pr_review_common.py")
        self.assertTrue(common_path.exists())
        self.assertFalse(SCRIPTS.joinpath("llm_pr_review.py").exists())
        self.assertFalse(SCRIPTS.joinpath("llm_review_prompt.md").exists())
        common_source = common_path.read_text(encoding="utf-8")
        self.assertNotIn("class LlmClient", common_source)
        self.assertNotIn("def main(", common_source)
        for name in ("pi_pr_review.py", "pi_pr_summary.py"):
            source = SCRIPTS.joinpath(name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("import pr_review_common as _review", source)
                self.assertNotIn("import llm_pr_review", source)


class PiPrReviewWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.self_hosted = WORKFLOWS.joinpath(
            "self-hosted-llm-pr-review.yml"
        ).read_text(encoding="utf-8")
        cls.pi_prompt = SCRIPTS.joinpath("pi_review_prompt.md").read_text(
            encoding="utf-8"
        )

    def test_workflow_uses_pi_review_entrypoint(self):
        self.assertIn("pi_pr_review.py", self.self_hosted)
        self.assertIn(
            "--prompt-path .github/scripts/pi_review_prompt.md",
            self.self_hosted,
        )
        self.assertNotIn("pr_review_common.py", self.self_hosted)

    def test_workflow_reviews_drafts_on_supported_events(self):
        self.assertNotIn("!github.event.pull_request.draft", self.self_hosted)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review]",
            self.self_hosted,
        )

    def test_workflow_keeps_self_hosted_configuration(self):
        expected = (
            "name: Pi PR Review",
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

    def test_workflow_labels_the_pi_review(self):
        self.assertIn(
            "LLM_REVIEW_LABEL: Pi review — 3 lenses, codebase context",
            self.self_hosted,
        )

    def test_prompt_uses_the_three_lens_contract(self):
        self.assertIn("{{LENS}}", self.pi_prompt)
        self.assertIn("trusted base checkout", self.pi_prompt)
        self.assertIn("line to null", self.pi_prompt)
        self.assertNotIn(
            "Prefer no finding over a speculative finding",
            self.pi_prompt,
        )

    def test_workflow_supports_a_trusted_manual_pr_canary(self):
        workflow = self.self_hosted
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

    def test_workflow_keeps_the_event_and_checkout_policy(self):
        expected = (
            "timeout-minutes: 20",
            "ref: ${{ steps.target.outputs.base_sha }}",
            "persist-credentials: false",
            "LLM_REVIEW_API_KEY: ${{ secrets.LLM_REVIEW_API_KEY }}",
            "LLM_REVIEW_BASE_URL: ${{ vars.LLM_REVIEW_BASE_URL }}",
            "LLM_REVIEW_MODEL: ${{ vars.LLM_REVIEW_MODEL }}",
        )
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.self_hosted)
        self.assertRegex(
            self.self_hosted,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )
        self.assertEqual(self.self_hosted.count("actions/checkout@"), 1)

    def test_environment_configuration_uses_shared_names(self):
        workflow = self.self_hosted
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


if __name__ == "__main__":
    unittest.main()
