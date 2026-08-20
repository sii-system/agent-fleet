import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / ".github" / "scripts" / "e2e_harbor_gate.py"

spec = importlib.util.spec_from_file_location("e2e_harbor_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def summary(
    status="complete",
    exit_code="0",
    total=89,
    completed=85,
    errored=3,
    cancelled=1,
    retries=2,
    mean_reward="0.42",
    counts=True,
    include_totals=True,
):
    lines = [
        f"status:      {status}",
        "finished_at: 2026-08-19T18:37:00Z",
        "RUN_ID:      e2e-1-1",
        "AGENT:       claude-code",
        "DATASET_NAME: terminal-bench/terminal-bench-2-1",
        "MODEL:       deepseekv4-flash-0731",
        f"harbor_exit_code: {exit_code}",
        "",
    ]
    if status != "complete":
        lines.extend(["failure_reason: Harbor exited without an aggregate result", ""])
    if not include_totals:
        lines.append("Harbor result summary: unavailable")
    else:
        lines.extend(
            [
                f"total:      {total}",
                f"completed:  {completed}",
                f"errored:    {errored}",
                f"cancelled:  {cancelled}",
                f"retries:    {retries}",
                f"mean_reward: {mean_reward}",
                "reward counts:",
            ]
        )
        if counts:
            lines.extend(["  reward=0.0: 50", "  reward=1.0: 35"])
        else:
            lines.append("  unavailable")
        lines.extend(["", "Harbor stats:", "{", '  "n_completed_trials": 85', "}"])
    lines.extend(
        ["", "result paths:", "  output:          /runs/x", "  job:             /jobs/x"]
    )
    return "\n".join(lines) + "\n"


class ParseSummaryTest(unittest.TestCase):
    def test_reads_column_zero_fields(self):
        fields = gate.parse_summary(summary())
        self.assertEqual(fields["status"], "complete")
        self.assertEqual(fields["total"], "89")
        self.assertEqual(fields["harbor_exit_code"], "0")
        self.assertEqual(fields["mean_reward"], "0.42")

    def test_ignores_labels_containing_spaces(self):
        # "reward counts:", "Harbor stats:" and "result paths:" are section
        # headings, not fields, and must not become keys.
        fields = gate.parse_summary(summary())
        for absent in ("reward", "Harbor", "result"):
            self.assertNotIn(absent, fields)

    def test_ignores_indented_lines(self):
        fields = gate.parse_summary(summary())
        self.assertNotIn("output", fields)

    def test_first_occurrence_wins(self):
        text = summary() + "status:      failed\n"
        self.assertEqual(gate.parse_summary(text)["status"], "complete")

    def test_missing_totals_block_yields_no_total(self):
        fields = gate.parse_summary(summary(include_totals=False))
        self.assertNotIn("total", fields)


class RewardCountsTest(unittest.TestCase):
    def test_reads_the_indented_reward_histogram(self):
        self.assertEqual(
            gate.parse_reward_counts(summary()), {"0.0": 50, "1.0": 35}
        )

    def test_unavailable_histogram_is_empty(self):
        self.assertEqual(gate.parse_reward_counts(summary(counts=False)), {})


class IntFieldTest(unittest.TestCase):
    def test_reads_an_integer(self):
        self.assertEqual(gate.int_field({"total": "89"}, "total"), 89)

    def test_absent_is_none(self):
        self.assertIsNone(gate.int_field({}, "total"))

    def test_non_numeric_is_none(self):
        self.assertIsNone(gate.int_field({"total": "many"}, "total"))


class EvaluateTest(unittest.TestCase):
    def test_a_healthy_run_passes(self):
        # 85 completed of 89, so 4 never completed; allowance is int(0.10*89)=8.
        verdict = gate.evaluate(summary())
        self.assertTrue(verdict.passed, verdict.reasons)
        self.assertEqual(verdict.stats["unresolved"], 4)

    def test_missing_summary_fails(self):
        verdict = gate.evaluate(None)
        self.assertFalse(verdict.passed)
        self.assertIn("summary.txt", verdict.reasons[0])

    def test_incomplete_status_fails_and_quotes_the_reason(self):
        verdict = gate.evaluate(summary(status="failed"))
        self.assertFalse(verdict.passed)
        self.assertTrue(any("not 'complete'" in r for r in verdict.reasons))
        self.assertTrue(any("aggregate result" in r for r in verdict.reasons))

    def test_nonzero_harbor_exit_code_fails(self):
        verdict = gate.evaluate(summary(exit_code="2"))
        self.assertFalse(verdict.passed)
        self.assertTrue(any("exited with code 2" in r for r in verdict.reasons))

    def test_absent_totals_block_fails(self):
        verdict = gate.evaluate(summary(include_totals=False))
        self.assertFalse(verdict.passed)
        self.assertTrue(any("no aggregate result" in r for r in verdict.reasons))

    def test_zero_trials_fails(self):
        verdict = gate.evaluate(
            summary(total=0, completed=0, errored=0, cancelled=0, counts=False)
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(any("no trials ran" in r for r in verdict.reasons))

    def test_a_retried_trial_counted_twice_still_passes(self):
        # Harbor counts a trial that errored then succeeded on retry in BOTH
        # n_errored_trials and n_completed_trials. The repo's own fixture
        # (test_harboropik_extra_compose.sh:118-123) is total=2, completed=2,
        # errored=1, so accounted=3 > total=2 on a healthy run. With
        # HARBOR_MAX_RETRIES defaulting to 2 this is the common case, not an
        # edge case.
        verdict = gate.evaluate(summary(total=2, completed=2, errored=1, cancelled=0, retries=1))
        self.assertTrue(verdict.passed, verdict.reasons)
        self.assertEqual(verdict.stats["unresolved"], 0)

    def test_a_full_nightly_that_recovers_every_error_passes(self):
        # 89 trials, 12 transient errors all retried to completion.
        verdict = gate.evaluate(
            summary(total=89, completed=89, errored=12, cancelled=0, retries=12)
        )
        self.assertTrue(verdict.passed, verdict.reasons)

    def test_missing_trials_fail(self):
        # A shortfall means trials vanished rather than being retried.
        verdict = gate.evaluate(summary(completed=10, errored=0, cancelled=0))
        self.assertFalse(verdict.passed)
        self.assertTrue(any("unaccounted for" in r for r in verdict.reasons))

    def test_never_completed_over_allowance_fails(self):
        # 9 of 89 never completed, exceeding the allowance of 8.
        verdict = gate.evaluate(summary(completed=80, errored=9, cancelled=0))
        self.assertFalse(verdict.passed)
        self.assertTrue(
            any("9 of 89 trials never completed" in r for r in verdict.reasons)
        )

    def test_never_completed_at_allowance_passes(self):
        # 8 of 89 is exactly the allowance and must not fail.
        verdict = gate.evaluate(summary(completed=81, errored=8, cancelled=0))
        self.assertTrue(verdict.passed, verdict.reasons)

    def test_cancelled_trials_count_as_never_completed(self):
        verdict = gate.evaluate(summary(total=89, completed=80, errored=0, cancelled=9))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stats["unresolved"], 9)

    def test_expected_trial_count_mismatch_fails(self):
        # Task selection silently not reaching Harbor: 89 ran, 4 requested.
        verdict = gate.evaluate(summary(), expected_trials=4)
        self.assertFalse(verdict.passed)
        self.assertTrue(any("expected 4 trials" in r for r in verdict.reasons))

    def test_expected_trial_count_match_passes(self):
        verdict = gate.evaluate(summary(), expected_trials=89)
        self.assertTrue(verdict.passed, verdict.reasons)

    def test_expected_trial_count_is_optional(self):
        verdict = gate.evaluate(summary(), expected_trials=None)
        self.assertTrue(verdict.passed, verdict.reasons)

    def test_failure_reason_quotes_counts_not_a_rounded_percentage(self):
        # 9/89 rounds to 10%, so a percentage would read "10% exceeds 10%".
        verdict = gate.evaluate(summary(completed=80, errored=9, cancelled=0))
        reason = next(r for r in verdict.reasons if "never completed" in r)
        self.assertNotIn("%", reason)

    def test_all_trials_unsolved_still_passes(self):
        # Every trial completed with reward 0: a model signal, not a fleet bug.
        text = summary(completed=89, errored=0, cancelled=0, mean_reward="0.0")
        verdict = gate.evaluate(text)
        self.assertTrue(verdict.passed, verdict.reasons)

    def test_reasons_accumulate(self):
        verdict = gate.evaluate(summary(status="failed", exit_code="3"))
        self.assertGreaterEqual(len(verdict.reasons), 2)

    def test_a_nonzero_shell_status_fails_an_otherwise_clean_run(self):
        # Isolates the shell-status check: zellij died after the summary landed.
        verdict = gate.evaluate(summary(), harbor_status=2)
        self.assertFalse(verdict.passed)
        self.assertTrue(
            any("exited with status 2" in r for r in verdict.reasons)
        )

    def test_tolerance_is_configurable(self):
        text = summary(completed=80, errored=9, cancelled=0)
        self.assertTrue(
            gate.evaluate(text, max_harness_failure_ratio=0.5).passed
        )


class RenderSummaryTest(unittest.TestCase):
    def test_pass_renders_a_table_and_the_reward_distribution(self):
        rendered = gate.render_summary(gate.evaluate(summary()))
        self.assertIn("PASS", rendered)
        self.assertIn("| Metric | Value |", rendered)
        self.assertIn("reward=0.0", rendered)

    def test_fail_lists_reasons(self):
        rendered = gate.render_summary(gate.evaluate(summary(status="failed")))
        self.assertIn("FAIL", rendered)
        self.assertIn("### Why this failed", rendered)

    def test_renders_without_a_summary_file(self):
        # stats is nearly empty here; every lookup must be guarded.
        rendered = gate.render_summary(gate.evaluate(None))
        self.assertIn("FAIL", rendered)


class MainTest(unittest.TestCase):
    def _run(self, tmp, text, extra=None):
        output = Path(tmp) / "runs" / "e2e-1-1"
        output.mkdir(parents=True)
        if text is not None:
            (output / "summary.txt").write_text(text, encoding="utf-8")
        step = Path(tmp) / "step.md"
        argv = ["--output-path", str(output), "--step-summary", str(step)]
        argv.extend(extra or [])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = gate.main(argv)
        return code, step.read_text(encoding="utf-8"), stderr.getvalue()

    def test_healthy_run_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, step, _ = self._run(tmp, summary())
        self.assertEqual(code, 0)
        self.assertIn("PASS", step)

    def test_broken_run_exits_one_and_annotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, step, err = self._run(tmp, summary(status="failed"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", step)
        self.assertIn("::error::", err)

    def test_absent_summary_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, step, _ = self._run(tmp, None)
        self.assertEqual(code, 1)
        self.assertIn("summary.txt", step)

    def test_writes_to_github_step_summary_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "runs" / "e2e-1-1"
            output.mkdir(parents=True)
            (output / "summary.txt").write_text(summary(), encoding="utf-8")
            step = Path(tmp) / "env-step.md"
            with mock.patch.dict(
                os.environ, {"GITHUB_STEP_SUMMARY": str(step)}
            ), contextlib.redirect_stdout(io.StringIO()):
                code = gate.main(["--output-path", str(output)])
            # Assert inside the with block: step lives in the temp dir.
            self.assertEqual(code, 0)
            self.assertIn("PASS", step.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
