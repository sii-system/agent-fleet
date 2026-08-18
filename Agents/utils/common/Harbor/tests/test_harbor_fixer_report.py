"""Tests for the deterministic human-readable Fixer report."""

from __future__ import annotations

import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixer_test_support import FixerTestCase, make_exec_result, make_fix_plan
from harbor_fixer.report import render_fix_report, write_fix_report


def _verification_result() -> dict:
    return {
        "schema_version": 2,
        "kind": "harbor_fixer_verification_result",
        "agent": "claude-code",
        "verification_mode": "smoke_test",
        "source": {},
        "execution": {"status": "success", "policy_status": "allowed"},
        "status": "fixed",
        "reason_codes": [],
        "rerun": {},
        "sampling": {"plan_task_count": 1},
        "new_run_summary": {},
        "plan_results": [],
        "task_results": [
            {
                "task": {
                    "task_index": "1",
                    "task_name": "task-1",
                    "attempt_id": None,
                },
                "verification_status": "fixed",
                "exec_status": "success",
                "exec_failure_reason": None,
                "new_run": {
                    "task_index": "1",
                    "task_name": "task-1",
                    "task_complete_status": "complete_success",
                },
            }
        ],
        "unexpected_run_task_results": [],
    }


class HarborFixerReportTest(FixerTestCase):
    def test_report_contains_summary_changes_and_remaining_issues(self) -> None:
        fix_plan = make_fix_plan()
        exec_result = make_exec_result(fix_plan=fix_plan)
        verification = _verification_result()

        report = render_fix_report("run-1", fix_plan, exec_result, verification)

        self.assertIn("# Harbor Fixer Report: run-1", report)
        self.assertIn("## Summary", report)
        self.assertIn("| Verification | fixed |", report)
        self.assertIn("| Reverification | 1 fixed |", report)
        self.assertIn("## Changes Applied", report)
        self.assertIn("Emit a harmless test line.", report)
        self.assertIn("## Remaining Issues", report)
        self.assertIn("No remaining issues were reported.", report)

    def test_write_report_publishes_the_rendered_markdown(self) -> None:
        fix_plan = make_fix_plan()
        output = self.root / "fix-report-latest.md"

        write_fix_report(
            "run-1",
            fix_plan,
            make_exec_result(fix_plan=fix_plan),
            _verification_result(),
            output,
        )

        self.assertEqual(
            output.read_text(encoding="utf-8"),
            render_fix_report(
                "run-1",
                fix_plan,
                make_exec_result(fix_plan=fix_plan),
                _verification_result(),
            ),
        )
