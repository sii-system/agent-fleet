import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-ci.yml"


class PrCiWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_uses_safe_pull_request_event(self):
        self.assertIn('"on":\n  pull_request:', self.workflow)
        self.assertNotIn("pull_request_target", self.workflow)
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            self.workflow,
        )

    def test_targets_the_self_hosted_linux_runner(self):
        self.assertIn(
            "runs-on: [self-hosted, Linux, X64]",
            self.workflow,
        )

    def test_uses_least_privilege_and_pinned_checkout(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertRegex(
            self.workflow,
            re.compile(r"uses: actions/checkout@[0-9a-f]{40}"),
        )

    def test_runs_the_fast_test_suites(self):
        for suite in ("tests", "scripts/tests", ".github/scripts/tests"):
            with self.subTest(suite=suite):
                self.assertIn(
                    f"python3 -m unittest discover -s {suite} -v",
                    self.workflow,
                )
        self.assertIn("bash scripts/tests/test_dind_run.sh", self.workflow)


if __name__ == "__main__":
    unittest.main()
