from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_pr_review as pi
import pi_review_replay as replay
from test_pi_pr_review import _make_text_response, _stub_pi_script


def verdict(status="confirmed"):
    return {
        "verdict": status,
        "rationale": "The zero input now divides by zero.",
        "failure_scenario": "Calling ratio(0) raises ZeroDivisionError.",
        "introduced_by_change": "The guard was removed in the head revision.",
        "counterevidence": "The caller accepts zero and no guard remains.",
        "evidence": [
            {"source_id": "s1", "start_line": 2, "end_line": 2, "quote": "    return 0 if n == 0 else 1 / n"},
            {"source_id": "s2", "start_line": 2, "end_line": 2, "quote": "    return 1 / n"},
        ],
    }


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.git("init", "-q")
        (self.repo / "ratio.py").write_text("def ratio(n):\n    return 0 if n == 0 else 1 / n\n")
        self.git("add", "ratio.py")
        self.git("commit", "-qm", "guarded")
        base = self.git("rev-parse", "HEAD")
        (self.repo / "ratio.py").write_text("def ratio(n):\n    return 1 / n\n")
        self.git("commit", "-qam", "remove guard")
        head = self.git("rev-parse", "HEAD")
        self.case = {
            "schema_version": 1, "id": "guard-removed", "repository": "example/repo",
            "base_sha": base, "head_sha": head, "expected_verdict": "confirmed",
            "finding": {"severity": "P1", "path": "ratio.py", "line": 2,
                        "title": "Zero input crashes", "failure_scenario": "ratio(0) raises",
                        "remediation": "Restore the guard"},
            "context": [
                {"revision": "base", "path": "ratio.py", "start_line": 1, "end_line": 2},
                {"revision": "head", "path": "ratio.py", "start_line": 1, "end_line": 2},
            ],
        }
        self.github = pi._review.GitHubClient("example/repo", "fake-github-token")
        self.client = pi.PiClient(str(self.bin / "pi"), "https://example.com/v1", "fake-model-token", "test-model", self.repo)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def run_case(self, payload=None):
        _stub_pi_script(self.bin, stdout=_make_text_response(json.dumps(payload or verdict())))
        with mock.patch.object(self.github, "create_review") as publish, mock.patch.object(self.github, "create_issue_comment") as comment:
            result = replay.run_replay(self.case, self.client, self.github)
        publish.assert_not_called()
        comment.assert_not_called()
        return result

    def test_replays_real_git_source_without_publication_or_label_leakage(self):
        self.case["label_notes"] = "HIDDEN_EVALUATOR_ONLY_SENTINEL"
        result = self.run_case()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification"]["verdict"], "confirmed")
        self.assertTrue(result["matches_expected"])
        self.assertEqual(result["base_sha"], self.case["base_sha"])
        self.assertEqual(result["model"], "test-model")
        self.assertTrue(result["prompt_sha256"])
        captured = (self.bin / "pi-capture.txt").read_text()
        self.assertNotIn("expected_verdict", captured)
        self.assertNotIn("HIDDEN_EVALUATOR_ONLY_SENTINEL", captured)
        self.assertIn("arg=<--no-tools>", captured)
        self.assertNotIn("fake-model-token", json.dumps(result))
        self.assertNotIn("fake-github-token", json.dumps(result))

    def test_fabricated_quote_cannot_be_confirmed(self):
        payload = verdict()
        payload["evidence"][1]["quote"] = "    raise RuntimeError('fabricated')"
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["model_verdict"], "confirmed")
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")
        self.assertTrue(result["verification"]["validation_errors"])
        self.assertFalse(result["matches_expected"])

    def test_claim_requires_evidence_from_both_revisions(self):
        payload = verdict()
        payload["evidence"] = payload["evidence"][1:]
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")

    def test_unchanged_source_cannot_establish_introduced_defect(self):
        self.case["base_sha"] = self.case["head_sha"]
        payload = verdict()
        payload["evidence"][0]["quote"] = payload["evidence"][1]["quote"]
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")

    def test_counterevidence_can_reject_candidate(self):
        self.case["head_sha"] = self.case["base_sha"]
        self.case["expected_verdict"] = "rejected"
        payload = verdict("rejected")
        payload["rationale"] = "The head retains the zero guard."
        payload["evidence"] = [dict(payload["evidence"][0], source_id="s2")]
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["verdict"], "rejected")
        self.assertTrue(result["matches_expected"])

    def test_missing_context_is_not_a_clean_verdict(self):
        payload = verdict("insufficient_evidence")
        payload["evidence"] = []
        payload["rationale"] = "The caller contract is unavailable."
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")

    def test_provider_failure_is_distinct_from_rejection_and_redacted(self):
        _stub_pi_script(self.bin, stderr="fake-model-token", exit_code=1)
        result = replay.run_replay(self.case, self.client, self.github)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("verification", result)
        self.assertNotIn("fake-model-token", json.dumps(result))
        self.assertIsNone(result["matches_expected"])

    def test_model_explanations_redact_supplied_credentials(self):
        payload = verdict()
        payload["rationale"] = "fake-model-token fake-github-token"
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["rationale"], "[REDACTED] [REDACTED]")

    def test_dummy_keys_preserve_artifact_schema_and_provenance(self):
        for key in ("a", "x", "completed", "confirmed", "s1", 'a"\\b'):
            with self.subTest(key=key):
                self.client.api_key = key
                payload = verdict()
                payload["rationale"] = key
                result = self.run_case(payload)
                self.assertEqual(result["schema_version"], 1)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["verification"]["verdict"], "confirmed")
                self.assertEqual(result["expected_verdict"], "confirmed")
                self.assertTrue(result["matches_expected"])
                self.assertEqual(result["head_sha"], self.case["head_sha"])
                self.assertEqual(result["sources"][0]["revision"], "base")
                self.assertEqual(result["sources"][0]["source_id"], "s1")
                self.assertEqual(result["verification"]["evidence"][0]["source_id"], "s1")
                self.assertEqual(result["verification"]["rationale"], "[REDACTED]")

    def test_output_length_errors_tell_repair_the_limit(self):
        for field in ("rationale", "failure_scenario", "quote"):
            with self.subTest(field=field):
                payload = verdict()
                if field == "quote":
                    payload["evidence"][0][field] = "a" * 2_001
                else:
                    payload[field] = "a" * 2_001
                with self.assertRaisesRegex(pi._review.ModelResponseError, "2000"):
                    replay.parse_verdict(payload)

    def test_dummy_key_does_not_corrupt_failed_status(self):
        self.client.api_key = "a"
        _stub_pi_script(self.bin, exit_code=1)
        result = replay.run_replay(self.case, self.client, self.github)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "verifier")

    def test_fetch_failure_cleans_storage_and_skips_verifier(self):
        directories = []

        def fail_fetch(_github, _sha, directory):
            directory.mkdir()
            (directory / "partial.pack").write_bytes(b"partial")
            directories.append(directory)
            raise pi.PiReviewError("fetch failed")

        with mock.patch.object(self.client, "prepare_source", side_effect=fail_fetch), mock.patch.object(self.client, "review") as verify:
            result = replay.run_replay(self.case, self.client, self.github)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "source")
        verify.assert_not_called()
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_unicode_separators_do_not_shift_git_line_numbers(self):
        (self.repo / "ratio.py").write_text('message = "one\u2028two"\nvalue = 2\n')
        self.git("commit", "-qam", "unicode")
        sha = self.git("rev-parse", "HEAD")
        source = replay._source(self.case["context"][1], sha, self.repo / ".git", "s1")
        self.assertEqual(source["text"], 'message = "one\u2028two"\nvalue = 2')

    def test_input_budget_failure_skips_verifier(self):
        with mock.patch.object(replay, "MAX_INPUT_BYTES", 10), mock.patch.object(self.client, "review") as verify:
            result = replay.run_replay(self.case, self.client, self.github)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "source")
        verify.assert_not_called()

    def test_invalid_context_is_rejected_before_model_call(self):
        for change in ({"path": "../secret"}, {"start_line": True}, {"end_line": 0}, {"revision": "main"}):
            with self.subTest(change=change):
                case = copy.deepcopy(self.case)
                case["context"][0].update(change)
                with self.assertRaises(ValueError):
                    replay.validate_case(case)

    def test_non_scalar_schema_fields_are_rejected(self):
        for field in ("expected_verdict",):
            case = copy.deepcopy(self.case)
            case[field] = []
            with self.assertRaises(ValueError):
                replay.validate_case(case)
        with self.assertRaises(pi._review.ModelResponseError):
            replay.parse_verdict(dict(verdict(), verdict=[]))

    def test_out_of_range_unknown_and_false_absence_citations_are_rejected(self):
        for citation in (
            {"source_id": "s2", "start_line": 3, "end_line": 3, "quote": "made up"},
            {"source_id": "unknown", "start_line": 2, "end_line": 2, "quote": "    return 1 / n"},
            {"source_id": "s2", "absent": True},
        ):
            with self.subTest(citation=citation):
                payload = verdict()
                payload["evidence"][1] = citation
                self.assertEqual(self.run_case(payload)["verification"]["verdict"], "insufficient_evidence")

    def test_tool_events_fail_the_tool_free_verifier(self):
        with mock.patch.object(self.client, "review", return_value=dict(verdict(), _pi_tool_calls=1)):
            result = replay.run_replay(self.case, self.client, self.github)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "verifier")

    def test_missing_file_is_explicit_and_cannot_fabricate_lines(self):
        self.case["context"][1]["path"] = "missing.py"
        result = self.run_case()
        self.assertEqual(result["sources"][1]["status"], "absent")
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")

    def test_new_file_can_use_verified_base_absence(self):
        (self.repo / "new.py").write_text("value = 1 / 0\n")
        self.git("add", "new.py")
        self.git("commit", "-qm", "new file")
        self.case["head_sha"] = self.git("rev-parse", "HEAD")
        self.case["finding"]["path"] = "new.py"
        self.case["finding"]["line"] = 1
        for spec in self.case["context"]:
            spec.update(path="new.py", start_line=1, end_line=1)
        payload = verdict()
        payload["evidence"] = [
            {"source_id": "s1", "absent": True},
            {"source_id": "s2", "start_line": 1, "end_line": 1, "quote": "value = 1 / 0"},
        ]
        result = self.run_case(payload)
        self.assertEqual(result["verification"]["verdict"], "confirmed")

    def test_large_blob_is_omitted_not_absent(self):
        with mock.patch.object(replay, "MAX_BLOB_BYTES", 8):
            result = self.run_case()
        self.assertEqual(result["sources"][1]["status"], "omitted")
        self.assertEqual(result["verification"]["verdict"], "insufficient_evidence")

    def test_cli_writes_private_artifact_and_never_overwrites_existing_output(self):
        case_file = self.root / "case.json"
        case_file.write_text(json.dumps(self.case))
        output = self.root / "artifact.json"
        _stub_pi_script(self.bin, stdout=_make_text_response(json.dumps(verdict())))
        args = ["--case", str(case_file), "--output", str(output), "--repository-root", str(self.repo), "--pi-bin", str(self.bin / "pi")]
        with mock.patch.dict("os.environ", {"LLM_REVIEW_BASE_URL": "https://example.com/v1", "LLM_REVIEW_MODEL": "test-model", "LLM_REVIEW_API_KEY": "fake-model-token"}):
            self.assertEqual(replay.main(args), 0)
            first = output.read_bytes()
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(replay.main(args), 1)
            self.assertEqual(output.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
