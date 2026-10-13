import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_helpers import step_script

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "harbor-e2e-validation.yml"


class E2eValidationWorkflowTest(unittest.TestCase):
    def test_publishes_redacted_joint_artifact_with_gate_fallback(self):
        script = step_script(WORKFLOW, "Publish run summary")
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

    def test_refreshes_and_exports_an_isolated_pinned_runner(self):
        script = step_script(WORKFLOW, "Validate prerequisites")
        for setup_status in (0, 1):
            with self.subTest(setup_status=setup_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "scripts").mkdir()
                (root / "scripts/prerequisites.sh").write_text(
                    "set -euo pipefail\n"
                    "agent_fleet_bootstrap_setup_prerequisites() { :; }\n"
                )
                setup = root / "Agents/utils/common/Harbor/setup_runner_env.sh"
                setup.parent.mkdir(parents=True)
                setup.write_text(
                    "set -euo pipefail\n"
                    'printf "%s\\n%s\\n" "$HARBOR_RUNNER_HOST_DIR" '
                    '"$HARBOR_RUNNER_IMAGE_DIR" > "$RUNNER_TEMP/setup-paths"\n'
                    f"exit {setup_status}\n"
                )
                binaries = root / "bin"
                binaries.mkdir()
                for name in ("docker", "python3.12"):
                    binary = binaries / name
                    binary.write_text("#!/bin/bash\nset -euo pipefail\nexit 0\n")
                    binary.chmod(0o755)
                temp = root / "temp"
                temp.mkdir()
                env_file = temp / "env"
                result = subprocess.run(
                    ["bash", "-c", script], cwd=root,
                    env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                         "PYTHON_BIN": "python3.12", "RUNNER_TEMP": str(temp),
                         "GITHUB_ENV": str(env_file)},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, setup_status, result.stderr)
                host, image = (temp / "setup-paths").read_text().splitlines()
                self.assertEqual(Path(host).parent, temp)
                self.assertEqual(Path(image).parent, temp)
                self.assertNotEqual(host, image)
                if setup_status:
                    self.assertFalse(env_file.exists())
                else:
                    self.assertIn(f"HARBOR_RUNNER_HOST_DIR={host}\n", env_file.read_text())
                    self.assertIn(f"HARBOR_RUNNER_IMAGE_DIR={image}\n", env_file.read_text())


if __name__ == "__main__":
    unittest.main()
