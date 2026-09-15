import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = [
    ROOT / '.github/workflows/harbor-e2e-validation.yml',
    ROOT / '.github/workflows/harbor-self-hosted-nightly.yml',
]
FIXTURE = (ROOT / '.github/scripts/tests/fixtures/harbor-joint-summary.md').read_text()


def step_script(workflow, name):
    step = workflow.read_text().split(f'      - name: {name}\n', 1)[1]
    return textwrap.dedent(step.split('\n      - name:', 1)[0].split('        run: |\n', 1)[1])


class SummaryPublishingTest(unittest.TestCase):
    def run_step(self, workflow, name, environment):
        return subprocess.run(
            ['bash', '-c', step_script(workflow, name)], cwd=ROOT,
            env={**os.environ, **environment}, capture_output=True, text=True,
            check=False,
        )

    def environment(self, root):
        run = root / 'run'
        run.mkdir()
        return {
            'API_KEY': 'fake-llm-secret-for-test', 'OPIK_API_KEY': 'fake-opik-secret-for-test',
            'GITHUB_WORKSPACE': str(ROOT), 'RUNNER_TEMP': str(root),
            'OUTPUT_PATH': str(run), 'GITHUB_OUTPUT': str(root / 'outputs'),
            'GITHUB_STEP_SUMMARY': str(root / 'summary.md'),
            'HEALTH_SUMMARY': str(root / 'health.md'), 'JOB_STATUS': 'failure',
            'GATE_OUTCOME': 'failure', 'STAGING_OUTCOME': 'failure',
            'STAGED_ARTIFACTS': '', 'ARTIFACT_OUTCOME': 'skipped', 'ARTIFACT_URL': '',
            'RUN_URL': 'https://example.invalid/run', 'PREPARED_SUMMARY': '',
        }

    def test_workflows_use_the_same_summary_preparation_and_presentation(self):
        for step in ('Prepare run summary', 'Publish run summary'):
            self.assertEqual(step_script(WORKFLOWS[0], step), step_script(WORKFLOWS[1], step))
        for workflow in WORKFLOWS:
            text = workflow.read_text()
            self.assertLess(text.index('- name: Prepare run summary'), text.index('- name: Stage and redact artifacts'))
            self.assertIn('PREPARED_SUMMARY: ${{ steps.summary.outputs.path }}', text)
            preparation = text.split('- name: Prepare run summary')[1].split('- name: Stage and redact artifacts')[0]
            self.assertIn("!cancelled() && steps.harbor.outcome != 'skipped'", preparation)
            self.assertNotIn('steps.stage.outcome', preparation)

    def test_failed_artifact_staging_still_publishes_the_redacted_joint_report(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.environment(root)
                source = Path(env['OUTPUT_PATH']) / 'summary.md'
                original = FIXTURE + '\n' + env['API_KEY'] + ' ' + env['OPIK_API_KEY']
                source.write_text(original)
                Path(env['HEALTH_SUMMARY']).write_text('The benchmark health gate failed.\n')
                prepared = self.run_step(workflow, 'Prepare run summary', env)
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                report = Path(env['GITHUB_OUTPUT']).read_text().strip().removeprefix('path=')
                env['PREPARED_SUMMARY'] = report
                self.assertEqual(source.read_text(), original)
                published = self.run_step(workflow, 'Publish run summary', env)
                self.assertEqual(published.returncode, 0, published.stderr)
                text = Path(env['GITHUB_STEP_SUMMARY']).read_text()
                for heading in ('# Benchmark Run Summary', '## Harbor', '## Analyzer', '## Fixer Results'):
                    self.assertIn(heading, text)
                self.assertIn('Run artifacts: unavailable', text)
                self.assertIn('<summary>CI pipeline validation: failure</summary>', text)
                self.assertIn('The benchmark health gate failed.', text)
                self.assertNotIn(env['API_KEY'], text)
                self.assertNotIn(env['OPIK_API_KEY'], text)
                self.assertNotIn('Joint summary.md unavailable', text)

    def test_missing_report_leaves_health_fallback_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.environment(Path(tmp))
            Path(env['HEALTH_SUMMARY']).write_text('CI failure details\n')
            prepared = self.run_step(WORKFLOWS[0], 'Prepare run summary', env)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertFalse(Path(env['GITHUB_OUTPUT']).exists())
            published = self.run_step(WORKFLOWS[0], 'Publish run summary', env)
            self.assertEqual(published.returncode, 0, published.stderr)
            text = Path(env['GITHUB_STEP_SUMMARY']).read_text()
            self.assertIn('Joint summary.md unavailable', text)
            self.assertIn('CI failure details', text)

    def test_failed_redaction_never_exposes_a_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.environment(Path(tmp))
            outside = Path(tmp) / 'outside.md'
            outside.write_text(FIXTURE)
            (Path(env['OUTPUT_PATH']) / 'summary.md').symlink_to(outside)
            result = self.run_step(WORKFLOWS[0], 'Prepare run summary', env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(env['GITHUB_OUTPUT']).exists())
            self.assertEqual(outside.read_text(), FIXTURE)

    def test_renderer_failure_falls_back_to_health(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.name), tempfile.TemporaryDirectory() as tmp:
                env = self.environment(Path(tmp))
                report = Path(tmp) / 'bad.md'
                report.write_bytes(b'\xff')
                env['PREPARED_SUMMARY'] = str(report)
                Path(env['HEALTH_SUMMARY']).write_text('CI failure details\n')
                result = self.run_step(workflow, 'Publish run summary', env)
                self.assertEqual(result.returncode, 0, result.stderr)
                text = Path(env['GITHUB_STEP_SUMMARY']).read_text()
                self.assertIn('could not be rendered', text)
                self.assertIn('CI failure details', text)
                self.assertNotIn('<summary>CI pipeline validation:', text)


if __name__ == '__main__':
    unittest.main()
