from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_pr_review as pi
import pi_review_verification as verification


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        (self.root / "ratio.py").write_text("def ratio(n):\n    return 0 if n == 0 else 1 / n\n")
        (self.root / "caller.py").write_text("from ratio import ratio\nprint(ratio(0))\n")
        self.git("add", "ratio.py", "caller.py")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        (self.root / "ratio.py").write_text("def ratio(n):\n    return 1 / n\n")
        self.git("commit", "-qam", "head")
        self.pull = {"base": {"sha": base}, "head": {"sha": self.git("rev-parse", "HEAD")}}
        self.github = pi._review.GitHubClient("example/repo", "fake-gh-key")
        self.client = pi.PiClient("pi", "https://example.com/v1", "fake-api-key", "test", self.root)
        self.raw = {"severity": "P1", "path": "ratio.py", "line": 2,
                    "title": "Zero input crashes", "failure_scenario": "Caller passes zero",
                    "remediation": "Restore the guard", "verification_context": [
                        {"revision": "head", "path": "caller.py", "start_line": 1, "end_line": 2}]}
        self.findings = pi._review.parse_findings({"findings": [self.raw]})[0]
        self.sources = []
        self.clients = []

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                              cwd=self.root, check=True, text=True, capture_output=True).stdout.strip()

    def response(self, client, prompt, model_input, **kwargs):
        self.clients.append(client)
        self.assertTrue(kwargs["no_tools"])
        self.assertLessEqual(client.timeout, 120)
        data = json.loads(model_input)
        self.sources = data["sources"]
        evidence = []
        for source in self.sources:
            if source["status"] == "available":
                evidence.append({"source_id": source["source_id"], "start_line": 2, "end_line": 2,
                                 "quote": source["text"].split("\n")[1]})
        return {"verdict": "confirmed", "rationale": "Zero reaches the removed guard",
                "failure_scenario": "Caller passes zero and crashes", "introduced_by_change": "Guard removed",
                "counterevidence": "No caller guard", "evidence": evidence, "_pi_tool_calls": 0}

    def run_verification(self, response=None, findings=None, payloads=None):
        with mock.patch.object(pi.PiClient, "review", autospec=True, side_effect=response or self.response):
            return verification.verify_candidates(
                self.github, self.client, self.pull,
                self.findings if findings is None else findings,
                [{"findings": [self.raw]}] if payloads is None else payloads,
                [{"filename": "ratio.py", "status": "modified"}],
            )

    def test_verifies_against_exact_revisions_and_unchanged_caller(self):
        result = self.run_verification()
        self.assertEqual(result[0]["verification"]["verdict"], "confirmed")
        self.assertEqual({s["revision"] for s in self.sources}, {"base", "head"})
        self.assertIn("caller.py", {s["path"] for s in self.sources})
        self.assertTrue(result[0]["sources"])
        self.assertTrue(all("text" not in source for source in result[0]["sources"]))
        self.assertTrue(all("commit_sha" in source for source in result[0]["sources"]))
        self.assertEqual(self.client.timeout, pi.PI_TIMEOUT_SECONDS)

    def test_source_stores_are_removed_after_verification(self):
        roots = []
        original = pi.PiClient.prepare_source
        def prepare(client, github, sha, root):
            roots.append(root)
            return original(client, github, sha, root)
        with mock.patch.object(pi.PiClient, "prepare_source", new=prepare):
            self.run_verification()
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_context_is_bounded_and_preserves_both_rename_paths(self):
        files = [{"filename": "ratio.py", "previous_filename": "old_ratio.py"}]
        self.raw["verification_context"] = [
            {"revision": "head", "path": f"caller{n}.py", "start_line": 1, "end_line": 20}
            for n in range(50)
        ]
        context = verification.candidate_context(self.findings[0], [{"findings": [self.raw]}], files)
        self.assertEqual(len(context), verification.MAX_CONTEXT)
        self.assertEqual({(item["revision"], item["path"]) for item in context[:4]},
                         {(r, p) for r in ("base", "head") for p in ("ratio.py", "old_ratio.py")})

    def test_script_entrypoint_reports_verifier_failure_without_aborting_review(self):
        from test_pi_pr_review import _make_text_response
        binary = self.root / "pi-stub"
        binary.write_text("#!/bin/bash\nset -euo pipefail\ncat >/dev/null\n"
                          'case " $* " in *" --no-tools "*) exit 1 ;; esac\n'
                          "cat <<'OUT'\n" + _make_text_response(json.dumps({"findings": [self.raw]})) + "\nOUT\n")
        binary.chmod(0o700)
        event = self.root / "event.json"
        event.write_text(json.dumps({"pull_request": {**self.pull, "number": 7}}))
        output = self.root / "report.json"
        scripts = Path(pi.__file__).parent
        wrapper = self.root / "run.py"
        wrapper.write_text("""import json, os, runpy, sys
sys.path.insert(0, os.environ['REVIEW_SCRIPTS'])
import llm_pr_review
class GitHub:
    def __init__(self, repository, token):
        self.api_root = 'https://api.github.com/repos/' + repository
        self.token = token
    def get_pull(self, number):
        with open(os.environ['REVIEW_EVENT']) as source:
            return json.load(source)['pull_request']
    def list_files(self, number):
        return [{'filename': 'ratio.py', 'patch': '@@ -1,2 +1,2 @@\\n def ratio(n):\\n-    return 0 if n == 0 else 1 / n\\n+    return 1 / n'}]
    def create_review(self, *args):
        raise AssertionError('no-publish must not write')
llm_pr_review.GitHubClient = GitHub
runpy.run_path(os.environ['REVIEW_SCRIPTS'] + '/pi_pr_review.py', run_name='__main__')
""")
        environment = dict(os.environ, REVIEW_SCRIPTS=str(scripts), REVIEW_EVENT=str(event),
                           GITHUB_REPOSITORY="example/repo", GITHUB_TOKEN="fake-gh-key",
                           GITHUB_WORKSPACE=str(self.root), LLM_REVIEW_API_KEY="fake-api-key",
                           LLM_REVIEW_MODEL="test", LLM_REVIEW_BASE_URL="https://example.com/v1")
        result = subprocess.run([sys.executable, str(wrapper), "--event-path", str(event),
                                 "--prompt-path", str(scripts / "pi_review_prompt.md"), "--pi-bin", str(binary),
                                 "--verification-mode", "enforce", "--no-publish", "--output", str(output)],
                                env=environment, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["verification_counts"]["failed"], 1)
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(report["findings"], [])

    def test_false_citation_is_not_confirmed(self):
        def respond(*args, **kwargs):
            result = self.response(*args, **kwargs)
            result["evidence"][0]["quote"] = "invented guard"
            return result
        result = self.run_verification(respond)
        self.assertEqual(result[0]["verification"]["verdict"], "insufficient_evidence")

    def test_invalid_context_never_reads_outside_git_tree(self):
        self.raw["verification_context"] += [
            {"revision": "head", "path": "../secret", "start_line": 1, "end_line": 2},
            {"revision": "head", "path": "/etc/passwd", "start_line": 1, "end_line": 2},
            {"revision": "head", "path": "caller.py", "start_line": True, "end_line": 2},
        ]
        self.run_verification()
        self.assertEqual({s["path"] for s in self.sources}, {"ratio.py", "caller.py"})

    def test_no_findings_needs_no_source_or_model(self):
        with mock.patch.object(self.client, "prepare_source") as prepare:
            self.assertEqual(self.run_verification(findings=[]), [])
        prepare.assert_not_called()

    def test_candidate_budget_is_visible_and_does_not_discard_candidates(self):
        with mock.patch.object(verification, "MAX_CANDIDATES", 1):
            result = self.run_verification(findings=self.findings * 2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["status"], "skipped")
        self.assertEqual(result[1]["reason"], "candidate_budget")
        self.assertEqual(len(self.clients), 1)

    def test_report_redaction_preserves_nested_source_identifiers(self):
        artifact = {"verification": [{"sources": [{"source_id": "s1"}],
                                     "verification": {"rationale": "s1 secret", "evidence": [{"source_id": "s1"}]}}]}
        result = verification.replay._redact_artifact(artifact, ("s1",))
        record = result["verification"][0]
        self.assertEqual(record["sources"][0]["source_id"], "s1")
        self.assertEqual(record["verification"]["evidence"][0]["source_id"], "s1")
        self.assertEqual(record["verification"]["rationale"], "[REDACTED] secret")

    def test_provider_failure_is_distinct_from_rejection(self):
        result = self.run_verification(mock.Mock(side_effect=pi.PiReviewError("fake-api-key")))
        self.assertEqual(result[0]["status"], "failed")
        self.assertNotIn("fake-api-key", json.dumps(result))

    def test_verifier_tool_use_cannot_confirm(self):
        def respond(*args, **kwargs):
            result = self.response(*args, **kwargs)
            result["_pi_tool_calls"] = 1
            return result
        self.assertEqual(self.run_verification(respond)[0]["status"], "failed")

    def test_source_failure_is_reported_without_calling_model(self):
        with mock.patch.object(pi.PiClient, "prepare_source", side_effect=pi.PiReviewError("fetch failed")):
            result = self.run_verification()
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(self.clients, [])

    def test_oversized_input_abstains_before_model(self):
        with mock.patch.object(verification.replay, "MAX_INPUT_BYTES", 1):
            result = self.run_verification()
        self.assertEqual(result[0]["status"], "skipped")
        self.assertEqual(result[0]["reason"], "input_budget")
        self.assertEqual(self.clients, [])


class VerificationOrchestrationTest(unittest.TestCase):
    def setUp(self):
        from test_pi_pr_review import FakeGitHub, FakePiClient
        self.github = FakeGitHub()
        self.client = FakePiClient()
        self.report = {}

    def run_mode(self, mode, verdict="rejected", **kwargs):
        record = {"status": "completed", "verification": {"verdict": verdict}}
        with mock.patch.object(verification, "verify_candidates", return_value=[record]):
            return pi.run_review(self.github, self.client, 7, "prompt", verification_mode=mode,
                                 report=self.report, **kwargs)

    def test_shadow_preserves_candidates_but_does_not_call_them_verified(self):
        self.assertEqual(self.run_mode("shadow"), "published")
        self.assertEqual(len(self.github.created[0][3]), 1)
        self.assertIn("candidate finding", self.github.created[0][2])
        self.assertIn("shadow", self.github.created[0][2])
        self.assertEqual(self.report["verification_counts"]["rejected"], 1)

    def test_enforce_removes_disproved_candidate(self):
        self.run_mode("enforce")
        self.assertEqual(self.github.created[0][3], [])
        self.assertEqual(self.report["withheld"], 1)

    def test_enforce_publishes_confirmed_candidate(self):
        self.run_mode("enforce", "confirmed")
        self.assertEqual(len(self.github.created[0][3]), 1)

    def test_unresolved_candidates_cannot_produce_a_clean_review(self):
        self.run_mode("enforce", "insufficient_evidence")
        summary = self.github.created[0][2]
        self.assertEqual(self.github.created[0][3], [])
        self.assertIn("Coverage: Partial", summary)
        self.assertNotIn("found no actionable findings", summary)
        self.assertIn("unresolved", summary)

    def test_no_publish_bypasses_duplicate_and_never_writes_github(self):
        with mock.patch.object(self.github, "list_reviews", side_effect=AssertionError("must not check duplicates")):
            self.assertEqual(self.run_mode("enforce", "confirmed", publish=False), "not-published")
        self.assertEqual(self.github.created, [])
        self.assertEqual(len(self.report["findings"]), 1)

    def test_failed_verifier_withholds_and_marks_partial(self):
        with mock.patch.object(verification, "verify_candidates", return_value=[{"status": "failed"}]):
            pi.run_review(self.github, self.client, 7, "prompt", verification_mode="enforce", report=self.report)
        self.assertEqual(self.github.created[0][3], [])
        self.assertIn("Coverage: Partial", self.github.created[0][2])
        self.assertEqual(self.report["verification_counts"]["failed"], 1)

    def test_invalid_mode_fails_before_discovery(self):
        with self.assertRaises(ValueError):
            pi.run_review(self.github, self.client, 7, "prompt", verification_mode="typo")
        self.assertEqual(self.client.inputs, [])

    def test_base_change_during_verification_prevents_publication(self):
        def verify(*args):
            self.github.pull = {**self.github.pull, "base": {"sha": "new-base"}}
            return [{"status": "completed", "verification": {"verdict": "confirmed"}}]
        with mock.patch.object(verification, "verify_candidates", side_effect=verify):
            result = pi.run_review(self.github, self.client, 7, "prompt", verification_mode="enforce")
        self.assertEqual(result, "stale")
        self.assertEqual(self.github.created, [])


class ReviewCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.event = self.root / "event.json"
        self.prompt = self.root / "prompt.md"
        self.output = self.root / "report.json"
        self.event.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": "b" * 40}, "base": {"sha": "a" * 40}}}))
        self.prompt.write_text("review prompt")
        self.environment = {"GITHUB_REPOSITORY": "example/repo", "GITHUB_TOKEN": "fake-gh-key",
                            "GITHUB_WORKSPACE": str(self.root), "LLM_REVIEW_API_KEY": "fake-api-key",
                            "LLM_REVIEW_BASE_URL": "https://example.com/v1", "LLM_REVIEW_MODEL": "test"}
        self.args = ["pi_pr_review.py", "--event-path", str(self.event), "--prompt-path", str(self.prompt),
                     "--verification-mode", "enforce", "--no-publish", "--output", str(self.output)]

    def test_no_publish_requires_output(self):
        with mock.patch.object(sys, "argv", self.args[:-2]), self.assertRaises(SystemExit):
            pi.parse_args()

    def test_output_is_private_redacted_and_does_not_overwrite(self):
        def review(*args, **kwargs):
            self.assertFalse(kwargs["publish"])
            self.assertEqual(kwargs["verification_mode"], "enforce")
            kwargs["report"]["findings"] = [{"title": "fake-api-key", "path": "fake-gh-key"}]
            return "not-published"
        with (mock.patch.object(sys, "argv", self.args),
              mock.patch.object(pi, "require_env", side_effect=self.environment.__getitem__),
              mock.patch.object(pi, "run_review", side_effect=review) as run):
            self.assertEqual(pi.main(), 0)
            self.assertEqual(pi.main(), 1)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.output.read_text())
        self.assertEqual(data["result"], "not-published")
        self.assertNotIn("fake-api-key", self.output.read_text())
        self.assertNotIn("fake-gh-key", self.output.read_text())

    def test_github_failure_still_records_failure(self):
        with mock.patch.object(sys, "argv", self.args), mock.patch.object(pi, "require_env", side_effect=self.environment.__getitem__), mock.patch.object(pi, "run_review", side_effect=OSError("failure")):
            self.assertEqual(pi.main(), 1)
        self.assertEqual(json.loads(self.output.read_text())["result"], "failed")

    def test_failed_run_still_records_failure(self):
        with mock.patch.object(sys, "argv", self.args), mock.patch.object(pi, "require_env", side_effect=self.environment.__getitem__), mock.patch.object(pi, "run_review", side_effect=pi.PiReviewError("failure")):
            self.assertEqual(pi.main(), 1)
        self.assertEqual(json.loads(self.output.read_text())["result"], "failed")


if __name__ == "__main__":
    unittest.main()
