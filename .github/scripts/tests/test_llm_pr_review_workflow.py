import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
PROMPT = ROOT / ".github" / "scripts" / "pi_review_prompt.md"


def _step_script(workflow: str, step_name: str) -> str:
    lines = workflow.splitlines()
    step_start = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == f"- name: {step_name}"
    )
    step_end = next(
        (
            index
            for index in range(step_start + 1, len(lines))
            if lines[index].strip().startswith("- name:")
        ),
        len(lines),
    )
    run_index = next(
        index
        for index in range(step_start + 1, step_end)
        if lines[index].strip() == "run: |"
    )
    body_indent = len(lines[run_index]) - len(lines[run_index].lstrip()) + 2
    return "\n".join(
        line[body_indent:] if line else ""
        for line in lines[run_index + 1 : step_end]
    ).rstrip()


class LlmPrReviewWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hosted = WORKFLOWS.joinpath("llm-pr-review.yml").read_text(
            encoding="utf-8"
        )
        cls.self_hosted = WORKFLOWS.joinpath(
            "self-hosted-llm-pr-review.yml"
        ).read_text(encoding="utf-8")
        cls.prompt = PROMPT.read_text(encoding="utf-8")

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
                self.assertIn("pi_pr_review.py", workflow)

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
            "LLM_REVIEW_ID: pi-pr-review",
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
            "LLM_REVIEW_ID: self-hosted-pi-pr-review",
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
            "--prompt-path .github/scripts/pi_review_prompt.md",
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
            self.assertEqual(workflow.count("pi_pr_review.py"), 1)

    def test_environment_configuration_uses_shared_names(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn("secrets.LLM_REVIEW_API_KEY", workflow)
                self.assertNotIn("secrets[", workflow)
                self.assertNotIn("vars[", workflow)
                self.assertNotIn("api_key_secret", workflow)
                self.assertNotIn("base_url_variable", workflow)
                self.assertNotIn("model_variable", workflow)

    def test_workflows_post_validate_the_tested_pi_version(self):
        for workflow in (self.hosted, self.self_hosted):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn('PI_VERSION: "0.81.1"', workflow)
                self.assertIn("INSTALLED_VERSION=", workflow)
                self.assertIn(
                    '[[ "$INSTALLED_VERSION" != "$PI_VERSION" ]]',
                    workflow,
                )
                self.assertIn("Expected pi version $PI_VERSION", workflow)
                self.assertIn(
                    'if ! INSTALLED_VERSION="$(pi --version 2>/dev/null)"; then',
                    workflow,
                )
                self.assertNotIn("grep -oE", workflow)
                self.assertNotIn(
                    "pi --version 2>/dev/null || true",
                    workflow,
                )

    def _run_version_step(
        self,
        script: str,
        initial_output: str | None,
        *,
        initial_exit_code: int = 0,
        post_install_output: str = "0.81.1\n",
        post_install_exit_code: int = 0,
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            output_path = root / "pi-output"
            exit_path = root / "pi-exit"
            calls_path = root / "npm-calls"
            pi_template = root / "pi-template"
            pi_template.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'cat "$FAKE_PI_OUTPUT"\n'
                'exit "$(cat "$FAKE_PI_EXIT")"\n',
                encoding="utf-8",
            )
            pi_template.chmod(0o755)
            if initial_output is not None:
                output_path.write_text(initial_output, encoding="utf-8")
                exit_path.write_text(str(initial_exit_code), encoding="utf-8")
                (bin_dir / "pi").write_bytes(pi_template.read_bytes())
                (bin_dir / "pi").chmod(0o755)
            npm = bin_dir / "npm"
            npm.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'printf "install\\n" >>"$FAKE_NPM_CALLS"\n'
                'cp "$FAKE_PI_TEMPLATE" "$FAKE_BIN/pi"\n'
                'chmod +x "$FAKE_BIN/pi"\n'
                'printf "%s" "$FAKE_PI_POST_OUTPUT" >"$FAKE_PI_OUTPUT"\n'
                'printf "%s" "$FAKE_PI_POST_EXIT" >"$FAKE_PI_EXIT"\n',
                encoding="utf-8",
            )
            npm.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                "PI_VERSION": "0.81.1",
                "FAKE_BIN": str(bin_dir),
                "FAKE_NPM_CALLS": str(calls_path),
                "FAKE_PI_EXIT": str(exit_path),
                "FAKE_PI_OUTPUT": str(output_path),
                "FAKE_PI_POST_EXIT": str(post_install_exit_code),
                "FAKE_PI_POST_OUTPUT": post_install_output,
                "FAKE_PI_TEMPLATE": str(pi_template),
            }
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            call_count = (
                len(calls_path.read_text(encoding="utf-8").splitlines())
                if calls_path.exists()
                else 0
            )
            return result, call_count

    def test_hosted_version_step_accepts_exact_without_install(self):
        script = _step_script(self.hosted, "Install pi coding agent")
        result, install_count = self._run_version_step(
            script, "0.81.1\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(install_count, 0)

    def test_hosted_version_step_reinstalls_when_exact_output_exits_nonzero(
        self,
    ):
        script = _step_script(self.hosted, "Install pi coding agent")
        result, install_count = self._run_version_step(
            script,
            "0.81.1\n",
            initial_exit_code=23,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(install_count, 1)

    def test_hosted_version_step_reinstalls_all_nonexact_versions(self):
        script = _step_script(self.hosted, "Install pi coding agent")
        cases = (
            None,
            "0.80.0\n",
            "0.81.1-beta.1\n",
            "0.81.1.1\n",
            "pi version 0.81.1\n",
        )
        for output in cases:
            with self.subTest(output=output):
                result, install_count = self._run_version_step(script, output)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(install_count, 1)

    def test_hosted_version_step_rejects_nonexact_post_install_output(self):
        script = _step_script(self.hosted, "Install pi coding agent")
        result, install_count = self._run_version_step(
            script,
            "0.80.0\n",
            post_install_output="0.81.1-beta.1\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_count, 1)
        self.assertIn("Expected pi version 0.81.1", result.stderr)

    def test_hosted_version_step_rejects_exact_failing_post_install(self):
        script = _step_script(self.hosted, "Install pi coding agent")
        result, install_count = self._run_version_step(
            script,
            "0.80.0\n",
            post_install_exit_code=23,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_count, 1)
        self.assertIn("Expected pi version 0.81.1", result.stderr)

    def test_self_hosted_version_step_accepts_only_exact_version(self):
        script = _step_script(
            self.self_hosted, "Verify pi is available"
        )
        exact, install_count = self._run_version_step(script, "0.81.1\n")
        self.assertEqual(exact.returncode, 0, exact.stderr)
        self.assertEqual(install_count, 0)

        cases = (
            None,
            "0.80.0\n",
            "0.81.1-beta.1\n",
            "0.81.1.1\n",
            "pi version 0.81.1\n",
        )
        for output in cases:
            with self.subTest(output=output):
                result, install_count = self._run_version_step(script, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(install_count, 0)
                self.assertIn("Expected pi version 0.81.1", result.stderr)

    def test_self_hosted_version_step_rejects_exact_failing_command(self):
        script = _step_script(
            self.self_hosted, "Verify pi is available"
        )
        result, install_count = self._run_version_step(
            script,
            "0.81.1\n",
            initial_exit_code=23,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_count, 0)
        self.assertIn("Expected pi version 0.81.1", result.stderr)

    def test_prompt_handles_files_absent_from_trusted_base(self):
        prompt = " ".join(self.prompt.split())
        self.assertIn(
            "only when that path exists in the trusted base checkout",
            prompt,
        )
        self.assertIn(
            "judge the added file from the supplied diff",
            prompt,
        )
        self.assertIn(
            "Path not found is not a reason to suppress a valid finding",
            prompt,
        )
        self.assertIn(
            "related existing callers, interfaces, and tests",
            prompt,
        )
        self.assertIn("Spend at most 4 tool calls per finding", self.prompt)
        self.assertIn("added RIGHT-side line", self.prompt)

    def test_reusable_workflow_is_removed(self):
        self.assertFalse(
            WORKFLOWS.joinpath("reusable-llm-pr-review.yml").exists()
        )


if __name__ == "__main__":
    unittest.main()
