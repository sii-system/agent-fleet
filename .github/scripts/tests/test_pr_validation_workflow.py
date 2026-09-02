import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-validation.yml"
OLD_WORKFLOW = ROOT / ".github" / "workflows" / "pr-ci.yml"


class PrValidationWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""
        )

    def test_replaces_the_disabled_quick_ci_workflow(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}")
        self.assertFalse(OLD_WORKFLOW.exists())
        self.assertIn("name: PR Validation", self.workflow)

    def test_runs_for_pull_requests_and_main_pushes(self):
        self.assertIn(
            '"on":\n  pull_request:\n  push:\n    branches: [main]',
            self.workflow,
        )
        self.assertNotIn("pull_request_target", self.workflow)

    def test_uses_a_hosted_runner_with_least_privilege(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("runs-on: ubuntu-24.04", self.workflow)
        self.assertNotIn("self-hosted", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)

    def test_pins_every_external_action(self):
        action_refs = re.findall(r"uses:\s+([^\s#]+)", self.workflow)
        pinned_action_refs = re.findall(
            r"uses:\s+([^\s@]+@[0-9a-f]{40})(?=\s|$)",
            self.workflow,
        )
        self.assertEqual(action_refs, pinned_action_refs)

    def test_cancels_superseded_runs(self):
        self.assertIn(
            "group: pr-validation-${{ github.event.pull_request.number || github.ref }}",
            self.workflow,
        )
        self.assertIn("cancel-in-progress: true", self.workflow)

    def test_validates_all_tracked_shell_syntax(self):
        self.assertIn("git ls-files -z '*.sh'", self.workflow)
        self.assertIn('bash -n "$script"', self.workflow)

    def test_installs_the_declared_harbor_runner_dependencies(self):
        self.assertIn(
            "uv pip install --system -r "
            "Agents/utils/common/Harbor/runner-requirements.txt",
            self.workflow,
        )
        self.assertNotIn("uv pip install --system PyYAML", self.workflow)

    def test_runs_all_python_test_suites(self):
        suites = (
            "tests",
            "scripts/tests",
            ".github/scripts/tests",
            "Agents/Harbor-claude-code/tests",
            "Agents/Harbor-opencode/tests",
            "Agents/Harbor-pi/tests",
            "Agents/Openclaw/tests",
            "Agents/utils/common/Harbor/tests",
            "Agents/utils/rl/tests",
            "Tasks/Pinchbench/tests",
            "Tasks/clawBio/tests",
        )
        for suite in suites:
            with self.subTest(suite=suite):
                self.assertIn(
                    f"python -m unittest discover -s {suite} -v",
                    self.workflow,
                )

    def test_runs_established_portable_shell_tests(self):
        scripts = (
            "scripts/tests/test_prerequisites.sh",
            "scripts/tests/test_dind_cgroup_v2.sh",
            "scripts/tests/test_dind_run.sh",
            "Agents/Openclaw/tests/test_build_openclaw_image.sh",
            "Agents/Openclaw/tests/test_session_layout.sh",
            "Agents/Openclaw/tests/test_start_session_tui.sh",
            "Agents/Openclaw/tests/test_stream_openclaw_session_sh.sh",
        )
        for script in scripts:
            with self.subTest(script=script):
                self.assertIn(f"bash {script}", self.workflow)


if __name__ == "__main__":
    unittest.main()
