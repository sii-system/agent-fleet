import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "self-hosted-llm-pr-review.yml"


class PiReviewCanaryWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_accepts_a_required_pull_request_number(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertRegex(
            self.workflow,
            re.compile(
                r"pr_number:\n"
                r"\s+description: Open pull request number to review\n"
                r"\s+required: true\n"
                r"\s+type: number"
            ),
        )

    def test_resolves_and_validates_the_manual_target(self) -> None:
        expected = (
            "name: Resolve review target",
            "inputs.pr_number",
            "api.github.com/repos",
            'pull.get("state") != "open"',
            'pull["base"]["repo"]["full_name"]',
            're.fullmatch(r"[0-9a-f]{40}", value)',
        )
        for value in expected:
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_resolves_target_with_isolated_python(self) -> None:
        self.assertIn("python3 -I - <<'PY'", self.workflow)

    def test_checks_out_the_resolved_trusted_base(self) -> None:
        self.assertIn("ref: ${{ steps.target.outputs.base_sha", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn(
            '--event-path "${{ steps.target.outputs.event_path }}"',
            self.workflow,
        )

    def test_does_not_define_an_unenforced_review_deadline(self) -> None:
        self.assertNotIn("PI_REVIEW_DEADLINE_EPOCH", self.workflow)

    def test_requires_a_containerized_code_review_runner(self) -> None:
        self.assertIn(
            "runs-on: [self-hosted, Linux, X64, container, code-review]",
            self.workflow,
        )

    def test_canary_has_a_unique_review_id_and_concurrency_group(self) -> None:
        self.assertIn("self-hosted-pi-pr-review${{", self.workflow)
        self.assertIn(
            "format('-canary-{0}-{1}', github.run_id, github.run_attempt)",
            self.workflow,
        )
        self.assertIn(
            "${{ github.event.pull_request.number }}-${{ github.event_name }}-",
            self.workflow,
        )
        self.assertIn("${{ inputs.pr_number }}", self.workflow)


if __name__ == "__main__":
    unittest.main()
