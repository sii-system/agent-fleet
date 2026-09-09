import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "harbor-e2e-validation.yml"
TASK_LIST = ROOT / "Tasks" / "Terminal-bench-2" / "harbor_terminalbench21_tasks.txt"

CANARY_TASKS = (
    "fix-git",
    "filter-js-from-html",
    "sqlite-db-truncate",
    "openssl-selfsigned-cert",
)


class E2eValidationWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_targets_a_bare_metal_runner(self):
        # The container runners are a label superset of [self-hosted, Linux, X64]
        # and have no Docker daemon, so the bare-metal label is required.
        self.assertIn(
            "runs-on: [self-hosted, Linux, X64, bare-metal]",
            self.workflow,
        )

    def test_runs_only_on_dispatch_and_schedule(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("schedule:", self.workflow)
        self.assertNotIn("pull_request", self.workflow)

    def test_schedule_avoids_the_top_and_half_hour(self):
        match = re.search(r'- cron: "(\d+) ', self.workflow)
        self.assertIsNotNone(match)
        self.assertNotIn(int(match.group(1)), (0, 30))

    def test_uses_the_self_hosted_environment(self):
        self.assertIn("environment: self-hosted-env", self.workflow)

    def test_uses_least_privilege_and_pinned_actions(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        for action in ("actions/checkout", "actions/upload-artifact"):
            with self.subTest(action=action):
                self.assertRegex(
                    self.workflow,
                    re.compile(rf"uses: {re.escape(action)}@[0-9a-f]{{40}} # v"),
                )

    def test_checks_out_submodules_for_opik_tracing(self):
        self.assertIn("submodules: recursive", self.workflow)

    def test_enables_tracing_with_an_opik_url_and_optional_api_key(self):
        # Self-hosted Opik may allow unauthenticated ingestion, so OPIK_URL is
        # the tracing switch and OPIK_API_KEY remains optional.
        self.assertNotIn("TRACE_TO_OPIK", self.workflow)
        self.assertIn("secrets.OPIK_API_KEY", self.workflow)
        self.assertIn(
            'if [[ -n "${OPIK_URL:-}" ]]; then',
            self.workflow,
        )
        self.assertIn('export OPIK_URL=""', self.workflow)

    def test_uses_hourly_tb21_opik_project_names(self):
        self.assertIn(
            'export OPIK_PROJECT_NAME="$(date -u +\'%Y-%m-%d-%H\')'
            '-harbor-e2e-validation-tb21"',
            self.workflow,
        )

    def test_redacts_every_credential_it_injects(self):
        redact = self.workflow[self.workflow.index("Stage and redact artifacts"):]
        for name in ("API_KEY", "OPIK_API_KEY"):
            with self.subTest(credential=name):
                self.assertIn(name, redact)

    def test_pins_the_output_path_for_producer_and_consumers(self):
        # A runner-level OUTPUT_ROOT/OUTPUT_PATH would otherwise win via
        # config_loader.sh. The path must also stay stable when start.sh changes
        # into the Harbor script directory before launching Zellij.
        self.assertIn(
            "printf 'output_path=%s/runs/%s\\n' \"$GITHUB_WORKSPACE\" \"$RUN_ID\"",
            self.workflow,
        )
        self.assertNotIn("printf 'output_path=runs/%s\\n'", self.workflow)
        self.assertIn(
            "OUTPUT_PATH: ${{ steps.params.outputs.output_path }}",
            self.workflow,
        )
        self.assertIn("export OUTPUT_PATH", self.workflow)

    def test_rejects_a_truncated_run(self):
        self.assertIn("expected_trials", self.workflow)
        self.assertIn("--expected-trials", self.workflow)

    def test_never_prunes_the_shared_docker_daemon(self):
        # These hosts also run the code-review container runners; a daemon-wide
        # prune deletes their stopped containers and dangling build layers.
        # Match executable lines only -- the comments explaining the absence of
        # these commands legitimately name them.
        code = [
            line
            for line in self.workflow.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for destructive in ("container prune", "image prune", "system prune"):
            with self.subTest(command=destructive):
                offenders = [line for line in code if destructive in line]
                self.assertEqual(offenders, [])

    def test_never_cancels_a_running_benchmark(self):
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_caps_job_duration(self):
        self.assertIn("timeout-minutes: 480", self.workflow)

    def test_preserves_cleanup_and_artifact_time_after_benchmark_budget(self):
        self.assertIn("BENCHMARK_TIMEOUT: 450m", self.workflow)
        self.assertIn(
            'timeout --signal=TERM --kill-after=5m "$BENCHMARK_TIMEOUT"',
            self.workflow,
        )
        self.assertIn('if [[ -f "$timeout_marker" ]]; then', self.workflow)
        self.assertIn('while kill -0 "$child" 2>/dev/null; do', self.workflow)

    def test_strips_the_chat_completions_suffix_from_the_gateway_url(self):
        # env.sh strips a trailing /v1 but not /chat/completions.
        self.assertIn('BASE_URL="${BASE_URL_RAW%/chat/completions}"', self.workflow)

    def test_bounds_per_task_time_and_enables_online_analysis(self):
        self.assertIn('HARBOR_TIMEOUT_MULTIPLIER: "1.0"', self.workflow)
        self.assertIn(
            'HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER: "2.0"', self.workflow
        )
        self.assertIn("AgentSetupTimeoutError", self.workflow)
        self.assertIn('HARBOR_ONLINE_ANALYSIS: "1"', self.workflow)

    def test_pins_zellij_cleanup_for_a_pty_run(self):
        # script(1) gives the step a pty, which would otherwise flip
        # start.sh's HARBOR_ZELLIJ_KEEP_ON_FAILURE default to 1.
        self.assertIn('HARBOR_ZELLIJ_KEEP_ON_FAILURE: "0"', self.workflow)

    def test_runs_harbor_under_script_to_allocate_a_pty(self):
        self.assertIn("script -e -q -c", self.workflow)

    def test_derives_workers_from_host_capacity(self):
        self.assertIn("e2e_worker_capacity.py", self.workflow)
        self.assertIn("steps.capacity.outputs.workers", self.workflow)

    def test_gates_on_pipeline_health(self):
        self.assertIn("e2e_harbor_gate.py", self.workflow)
        self.assertIn("--max-harness-failure-ratio 0.10", self.workflow)

    def test_summary_runs_after_cleanup_even_when_checkout_fails(self):
        self.assertIn("- name: Publish run summary\n        if: always()", self.workflow)
        self.assertLess(self.workflow.index("- name: Clean up"),
                        self.workflow.index("- name: Publish run summary"))
        self.assertIn("--step-summary", self.workflow)
        self.assertIn("steps.artifacts.outputs.artifact-url", self.workflow)

    def test_publishes_redacted_joint_artifact_with_gate_fallback(self):
        script = textwrap.dedent(self.workflow.split("- name: Publish run summary")[1]
                                 .split("        run: |\n")[1])
        joint = (ROOT / ".github/scripts/tests/fixtures/harbor-joint-summary.md").read_text()
        without_fixer = joint.split("\n## Fixer Results\n")[0] + "\n"
        for staging, report, gate_report, upload in (
            ("success", joint, "CI PASS", "success"),
            ("success", without_fixer, "CI FAIL", "success"),
            ("success", joint, "CI PASS", "failure"),
            ("failure", joint, "CI FAIL", "skipped"),
            ("skipped", joint, "", "skipped"),
            ("success", "", "CI PASS", "success"),
            ("skipped", "", "", "skipped"),
        ):
            with self.subTest(staging=staging, report=bool(report), upload=upload), tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / "staged"
                staged.mkdir()
                (staged / "summary.md").write_text(report)
                gate = Path(tmp) / "gate.md"
                gate.write_text(gate_report)
                destination = Path(tmp) / "summary.md"
                artifact_url = "https://example.com/artifact" if upload == "success" else ""
                gate_outcome = {"CI PASS": "success", "CI FAIL": "failure", "": "skipped"}[gate_report]
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True, check=False,
                    cwd=ROOT if staging == "success" else tmp,
                    env={**os.environ, "HEALTH_SUMMARY": str(gate),
                         "GITHUB_STEP_SUMMARY": str(destination), "JOB_STATUS": "failure",
                         "GATE_OUTCOME": gate_outcome,
                         "STAGING_OUTCOME": staging, "STAGED_ARTIFACTS": str(staged),
                         "ARTIFACT_URL": artifact_url, "ARTIFACT_OUTCOME": upload,
                         "RUN_URL": "https://example.com/run"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((staged / "summary.md").read_text(), report)
                rendered = destination.read_text()
                self.assertIn("[Run logs](https://example.com/run)", rendered)
                if upload == "success":
                    self.assertIn("[Download run artifacts](https://example.com/artifact)", rendered)
                else:
                    self.assertIn(f"Run artifacts: unavailable (upload step: {upload}).", rendered)
                    self.assertNotIn("[Download run artifacts]", rendered)
                if staging == "success" and report:
                    if report == joint:
                        location = "`fixer/fix-report-latest.md`"
                        location += (f" ([download run artifacts]({artifact_url}))" if artifact_url
                                     else " (artifact upload unavailable)")
                        self.assertIn(joint.replace("[fix-report-latest.md](fixer/fix-report-latest.md)", location), rendered)
                    else:
                        self.assertIn(without_fixer, rendered)
                        self.assertNotIn("## Fixer Results", rendered)
                else:
                    self.assertNotIn("A required dependency was unavailable.", rendered)
                    self.assertIn("Joint summary.md unavailable", rendered)
                if gate_report:
                    self.assertIn(gate_report, rendered)
                else:
                    self.assertIn("Pipeline validation report unavailable", rendered)

    def test_redacts_artifacts_before_upload(self):
        self.assertIn("Stage and redact artifacts", self.workflow)
        redact_index = self.workflow.index("Stage and redact artifacts")
        upload_index = self.workflow.index("Upload run artifacts")
        self.assertLess(redact_index, upload_index)

    def test_refuses_to_redact_with_an_empty_key(self):
        # bytes.replace(b"", ...) would corrupt every artifact and
        # grep -F "" would match every file, so both uses must be guarded.
        self.assertEqual(self.workflow.count('if [[ -z "$API_KEY" ]]; then'), 2)

    def test_pins_a_deterministic_zellij_session_name(self):
        # env.sh's default session name contains no RUN_ID, so cleanup
        # could not otherwise find the session it needs to kill.
        self.assertEqual(
            self.workflow.count(
                "ZELLIJ_SESSION_NAME: ${{ steps.params.outputs.run_id }}"
            ),
            2,
        )

    def test_does_not_use_interactive_setup(self):
        # setup.sh prompts for missing credentials and would persist the key.
        self.assertNotIn("scripts/setup.sh", self.workflow)

    def test_canary_tasks_exist_in_the_terminalbench21_task_list(self):
        available = set(TASK_LIST.read_text(encoding="utf-8").split())
        for task in CANARY_TASKS:
            with self.subTest(task=task):
                self.assertIn(task, available)

    def test_canary_tasks_are_the_documented_dispatch_default(self):
        self.assertIn("CANARY_TASKS: " + ",".join(CANARY_TASKS), self.workflow)


if __name__ == "__main__":
    unittest.main()
