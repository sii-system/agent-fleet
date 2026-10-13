"""Keep workflow security policies without duplicating workflow configuration."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"


class WorkflowPolicyTest(unittest.TestCase):
    def test_actions_are_pinned_and_checkout_does_not_persist_credentials(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    action = step.get("uses")
                    if not action or action.startswith("./"):
                        continue
                    with self.subTest(workflow=path.name, action=action):
                        self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
                        if action.startswith("actions/checkout@"):
                            self.assertIs(step["with"]["persist-credentials"], False)

    def test_pr_checks_use_hosted_runners_and_read_only_permissions(self):
        for name in ("pr-validation.yml", "ruff.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((WORKFLOWS / name).read_text())
                self.assertIn("pull_request", workflow["on"])
                self.assertNotIn("pull_request_target", workflow["on"])
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                for job in workflow["jobs"].values():
                    self.assertTrue(job["runs-on"].startswith("ubuntu-"))
                    self.assertNotIn("permissions", job)

    def test_pr_automation_checks_out_the_trusted_base(self):
        for name, trusted_ref in (
            ("self-hosted-llm-pr-review.yml", "${{ steps.target.outputs.base_sha || github.event.pull_request.base.sha }}"),
            ("pi-pr-summary.yml", "${{ github.event.pull_request.base.sha }}"),
        ):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((WORKFLOWS / name).read_text())
                self.assertEqual(
                    workflow["permissions"], {"contents": "read", "pull-requests": "write"}
                )
                for job in workflow["jobs"].values():
                    self.assertNotIn("permissions", job)
                    if name == "self-hosted-llm-pr-review.yml":
                        self.assertTrue({"self-hosted", "container", "code-review"}.issubset(job["runs-on"]))
                    checkouts = [
                        step for step in job["steps"]
                        if step.get("uses", "").startswith("actions/checkout@")
                    ]
                    self.assertEqual(len(checkouts), 1)
                    self.assertEqual(
                        re.sub(r"\s+", "", checkouts[0]["with"]["ref"]),
                        re.sub(r"\s+", "", trusted_ref),
                    )

    def test_benchmarks_preserve_shared_hosts_and_redact_before_upload(self):
        for name in ("harbor-e2e-validation.yml", "harbor-self-hosted-nightly.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((WORKFLOWS / name).read_text())
                self.assertEqual(set(workflow["on"]), {"workflow_dispatch", "schedule"})
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertIs(workflow["concurrency"]["cancel-in-progress"], False)
                for job in workflow["jobs"].values():
                    self.assertIn("bare-metal", job["runs-on"])
                    steps = job["steps"]
                    names = [step.get("name") for step in steps]
                    self.assertLess(names.index("Clean up"), names.index("Stage and redact artifacts"))
                    self.assertLess(names.index("Stage and redact artifacts"), names.index("Upload run artifacts"))
                    upload = next(step for step in steps if step.get("id") == "artifacts")
                    self.assertIn("steps.stage.outcome == 'success'", upload["if"])
                    self.assertEqual(steps[-1]["if"], "always()")
                    for step in steps:
                        code = "\n".join(
                            line for line in step.get("run", "").splitlines()
                            if not line.lstrip().startswith("#")
                        )
                        self.assertIsNone(re.search(r"\bdocker\s+(?:container|image|system)\s+prune\b", code))


if __name__ == "__main__":
    unittest.main()
