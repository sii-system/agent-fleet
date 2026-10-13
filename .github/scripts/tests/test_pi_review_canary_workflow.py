import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_helpers import step_script, workflow_step

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "self-hosted-llm-pr-review.yml"


class PiReviewCanaryWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver_script = step_script(WORKFLOW, "Resolve review target")

    def test_report_upload_is_bounded_and_does_not_override_review_outcome(self) -> None:
        upload = workflow_step(WORKFLOW, "Save review evaluation report")
        self.assertIs(upload["continue-on-error"], True)
        self.assertEqual(upload["timeout-minutes"], 2)
        self.assertEqual(upload["if"], "always()")
        review = workflow_step(WORKFLOW, "Review pull request with pi agent")
        self.assertNotIn("continue-on-error", review)

    def test_automatic_target_uses_the_runner_event_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            event_path = root / "event.json"
            output_path = root / "output"
            # The resolver must ignore Python startup code from the caller.
            (root / "sitecustomize.py").write_text('raise SystemExit("untrusted startup")\n')
            base_sha = "a" * 40
            head_sha = "b" * 40
            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "base": {"sha": base_sha},
                            "head": {"sha": head_sha},
                        }
                    }
                ),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "EVENT_NAME": "pull_request_target",
                    "EVENT_PATH": "",
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_OUTPUT": str(output_path),
                    "PYTHONPATH": str(root),
                }
            )

            completed = subprocess.run(
                ["bash", "-c", self.resolver_script],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                f"base_sha={base_sha}\nevent_path={event_path}\n",
            )


if __name__ == "__main__":
    unittest.main()
