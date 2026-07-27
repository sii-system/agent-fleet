import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "ruff.yml"


class RuffWorkflowContractTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}")
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_runs_for_pull_requests_and_main_pushes(self):
        self.assertIn(
            '"on":\n  pull_request:\n  push:\n    branches: [main]',
            self.workflow,
        )
        self.assertNotIn("pull_request_target", self.workflow)

    def test_uses_a_hosted_runner_with_least_privilege(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("runs-on: ubuntu-latest", self.workflow)
        self.assertNotIn("secrets.", self.workflow)

    def test_pins_checkout_and_ruff_actions(self):
        self.assertIn(
            "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
            " # v7.0.1",
            self.workflow,
        )
        self.assertIn(
            "uses: astral-sh/ruff-action@"
            "278981a28ce3188b1e39527901f38254bf3aac89 # v4.1.0",
            self.workflow,
        )
        action_refs = re.findall(r"uses:\s+([^\s#]+)", self.workflow)
        pinned_action_refs = re.findall(
            r"uses:\s+([^\s@]+@[0-9a-f]{40})(?=\s|$)",
            self.workflow,
        )
        self.assertEqual(action_refs, pinned_action_refs)
        self.assertIn("persist-credentials: false", self.workflow)

    def test_runs_only_pinned_ruff_lint_with_the_repository_config(self):
        self.assertIn('version: "0.16.0"', self.workflow)
        self.assertIn(
            'args: "check --config .github/ruff.toml"',
            self.workflow,
        )
        self.assertNotIn("--fix", self.workflow)
        self.assertNotIn("format", self.workflow.lower())


if __name__ == "__main__":
    unittest.main()
