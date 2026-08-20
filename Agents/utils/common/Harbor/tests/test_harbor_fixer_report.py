"""Tests for deterministic and agent-generated Harbor Fixer reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
for path in (TEST_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import write_benchmark_summary as summary_writer
from fixer_test_support import (
    FixerTestCase,
    make_exec_result,
    make_fix_plan,
    write_analyzer_fixture,
    write_json,
)
from harbor_fixer.analyzer_inputs import resolve_analyzer_paths
from harbor_fixer.report import (
    generate_report_from_paths,
    generate_report_summary,
    render_fix_report,
    run_report_from_paths,
    write_fix_report,
    write_report_markdown,
)
from harbor_fixer.validation import (
    ValidationError,
    validate_fix_report,
    validate_report_summary,
)
from harbor_fixer.verifier import run_verification_from_paths


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


class _SequenceInvoker:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.payloads: list[dict] = []
        self.prompts: list[str] = []

    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        self.payloads.append(payload)
        self.prompts.append(prompt)
        return self.outputs[attempt - 1]


class _FailingInvoker:
    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        raise TimeoutError("provider API_KEY=fake-secret timed out")


def _write_generation_fixture(root: Path) -> tuple[Path, Path, Path]:
    analyzer_dir = write_analyzer_fixture(root, count=2)
    analyzer_paths = resolve_analyzer_paths(analyzer_dir)
    output_dir = root / "fixer"
    plan_path = root / "fix-plan-latest.json"
    exec_path = root / "exec-result-latest.json"
    plan = make_fix_plan()
    plan["source"].update(
        {
            "agent": "claude-code",
            "monitor_path": str(root / "monitor" / "monitor-latest.json"),
            "analyzer_root": str(analyzer_paths["analyzer_root"]),
            "manifest_path": str(analyzer_paths["manifest_path"]),
            "run_id": analyzer_paths["run_id"],
            "publications": analyzer_paths["publications"],
        }
    )
    write_json(plan_path, plan)
    write_json(exec_path, make_exec_result(fix_plan=plan))
    run_dir = root / "verification-run"
    queue_dir = run_dir / "queue" / "claude-code"
    queue_dir.mkdir(parents=True)
    (run_dir / "tasks.txt").write_text("task-1\n", encoding="utf-8")
    (queue_dir / "done.txt").write_text(
        "1\ttask-1\t1.0\t\t\n", encoding="utf-8"
    )
    (queue_dir / "failed.txt").write_text("", encoding="utf-8")
    run_verification_from_paths(
        Path(os.path.relpath(plan_path)),
        Path(os.path.relpath(exec_path)),
        run_dir,
        output_dir,
        agent="claude-code",
        monitor_policy="off",
    )
    return analyzer_dir, output_dir, exec_path


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

    def test_runtime_retries_invalid_summary_and_has_factual_fallback(self) -> None:
        valid = {
            "schema_version": 1,
            "kind": "harbor_fixer_report_summary",
            "status": "success",
            "text": "One sampled task was fixed.",
            "highlights": [],
            "caveats": [],
            "generation_errors": [],
        }
        summary_input = {
            "schema_version": 1,
            "kind": "harbor_fixer_report_summary_input",
            "old_run": {"monitor_available": False},
            "new_run": {
                "verification_mode": "smoke_test",
                "sampling": {
                    "plan_task_count": 2,
                    "sampled_task_count": 1,
                    "unsampled_task_count": 1,
                },
            },
            "task_results": [
                {"sampled": True, "verification_status": "fixed"},
                {"sampled": False, "verification_status": "exec_failed"},
            ],
            "caveats": [],
        }
        with self.assertRaises(ValidationError):
            validate_report_summary({**valid, "highlights": [{"task": "task-1"}]})
        with self.assertRaises(ValidationError):
            validate_report_summary({**valid, "detail": {"task": "task-1"}})
        for invalid_text in ("", " \n\t "):
            with self.subTest(invalid_text=invalid_text), self.assertRaises(
                ValidationError
            ):
                validate_report_summary({**valid, "text": invalid_text})
        with self.assertRaises(ValidationError):
            validate_report_summary({**valid, "caveats": [{}]})
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"HARBOR_AGENT_RETRY_INITIAL_SECONDS": "0"}
        ):
            invoker = _SequenceInvoker(
                [
                    json.dumps({**valid, "status": "failed", "text": ""}),
                    json.dumps(valid),
                ]
            )
            summary, _ = generate_report_summary(
                invoker, summary_input, Path(root) / "valid"
            )
            fallback, _ = generate_report_summary(
                _FailingInvoker(), summary_input, Path(root) / "fallback"
            )
            blocked_output = Path(root) / "blocked"
            blocked_output.write_text("not a directory", encoding="utf-8")
            write_invoker = _SequenceInvoker([json.dumps(valid)])
            with self.assertRaises(OSError):
                generate_report_summary(
                    write_invoker, summary_input, blocked_output, max_attempts=2
                )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(len(summary["generation_errors"]), 1)
        self.assertIn("status must be success", summary["generation_errors"][0]["error"])
        self.assertIn("Validation retry", invoker.prompts[1])
        self.assertEqual(fallback["status"], "failed")
        self.assertIn("1 of 2 planned task(s)", fallback["text"])
        self.assertIn("1 task(s) were not sampled", fallback["text"])
        self.assertIn("1 unsampled task(s) labeled exec_failed", fallback["text"])
        self.assertIn("Baseline monitor data was unavailable", fallback["text"])
        self.assertNotIn("fake-secret", json.dumps(fallback))
        self.assertEqual(len(write_invoker.prompts), 1)

    def test_generation_redacts_rerun_secrets(self) -> None:
        analyzer_dir, output_dir, _ = _write_generation_fixture(self.root)
        verification_path = output_dir / "verification-result-latest.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        verification["rerun"].update(
            {
                "command": "runner --password 'top'\"'\"'fake-secret value'",
                "stdout_summary": "API_KEY=fake-secret",
                "stderr_summary": "token=fake-secret",
            }
        )
        write_json(verification_path, verification)
        invoker = _SequenceInvoker(
            [
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "harbor_fixer_report_summary",
                        "status": "success",
                        "text": "Fixture report.",
                        "highlights": [],
                        "caveats": [],
                        "generation_errors": [],
                    }
                )
            ]
        )

        with (
            mock.patch.dict(os.environ, {"HOME": str(self.root)}),
            mock.patch(
                "harbor_fixer.report.generation.resolve_analyzer_paths",
                wraps=resolve_analyzer_paths,
            ) as resolve,
        ):
            result = run_report_from_paths(
                verification_path,
                analyzer_dir,
                Path("~/fixer"),
                invoker,
                baseline_run_dir=self.root / "renamed-baseline",
                baseline_monitor_policy="off",
            )

        validate_fix_report(result, verification_result=verification)
        report_input_path = output_dir / "report-input.json"
        report_input = report_input_path.read_text(encoding="utf-8")
        self.assertNotIn("fake-secret", json.dumps(result))
        self.assertNotIn("fake-secret", json.dumps(invoker.payloads))
        self.assertNotIn("fake-secret", report_input)
        self.assertEqual(resolve.call_count, 1)
        self.assertTrue(Path(verification["source"]["fix_plan_path"]).is_absolute())
        self.assertTrue(Path(verification["source"]["exec_result_path"]).is_absolute())
        self.assertEqual(os.stat(report_input_path).st_mode & 0o777, 0o600)
        self.assertEqual(
            os.stat(output_dir / "fix-report-latest.json").st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            result["artifacts"]["human_report_path"],
            str(output_dir / "fix-report-latest.md"),
        )
        markdown = (output_dir / "fix-report-latest.md").read_text(encoding="utf-8")
        self.assertNotIn("runner --password", markdown)
        self.assertNotIn("token=<REDACTED>", markdown)
        self.assertNotIn("fake-secret", markdown)
        invalid = {**result, "task_results": ["invalid"]}
        with self.assertRaisesRegex(ValidationError, "task_results"):
            validate_fix_report(invalid)

    def test_generation_rejects_mismatched_runtime_sources(self) -> None:
        analyzer_dir, output_dir, exec_path = _write_generation_fixture(self.root)
        verification_path = output_dir / "verification-result-latest.json"
        exec_result = json.loads(exec_path.read_text(encoding="utf-8"))
        exec_result["plans"][0]["actions"][0]["later_execution"] = True
        write_json(exec_path, exec_result)
        stale_markdown = output_dir / "fix-report-latest.md"
        stale_markdown.write_text("stale\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "verification result"):
            generate_report_from_paths(
                verification_path,
                analyzer_dir,
                output_dir,
                _FailingInvoker(),
                baseline_monitor_policy="off",
            )
        self.assertFalse(stale_markdown.exists())

        write_json(
            exec_path,
            make_exec_result(
                fix_plan=json.loads(
                    (self.root / "fix-plan-latest.json").read_text(encoding="utf-8")
                )
            ),
        )
        wrong_snapshot = {
            "analyzer_handover": {"run_id": "other-run", "agent": "claude-code"}
        }
        with mock.patch(
            "harbor_fixer.report.generation.read_monitor_snapshot",
            return_value=(wrong_snapshot, "snapshot.json"),
        ), self.assertRaisesRegex(ValidationError, "baseline monitor"):
            generate_report_from_paths(
                verification_path,
                analyzer_dir,
                output_dir,
                _FailingInvoker(),
                baseline_run_dir=self.root / "baseline",
                baseline_monitor_policy="on",
            )

    def test_markdown_is_deterministic_redacted_view_of_report_artifacts(self) -> None:
        output_dir = self.root / "markdown"
        plan_path = output_dir / "fix-plan-latest.json"
        exec_path = output_dir / "exec-result-latest.json"
        plan = make_fix_plan()
        plan["plans"][0]["plan_id"] += "\n## Forged plan id"
        plan["plans"][0]["actions"][0]["action_id"] += "\n## Forged action id"
        plan["plans"][0]["actions"][0]["arguments"] = [
            "API_KEY=top secret value",
            "--password",
            "top'secret value",
            "literal`````text",
        ]
        plan["plans"][0]["fix_reason"].update(
            summary="## Forged plan summary\nFORGED PLAN",
            reasoning="## Forged plan reasoning\nFORGED REASON",
        )
        write_json(plan_path, plan)
        secrets = [
            "API_KEY=top secret value",
            "--api-key top secret value",
            "ghp_abcdefghijklmnop",
            "sk-abcdefghijklmnop",
            "Bearer 'abcdefghijklmnop'",
            "Authorization: Basic dXNlcjpwYXNz",
            '{"Authorization": "Basic dXNlcjpwYXNz"}',
            "https://user:password@example.invalid/path",
            "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----",
        ]
        exec_result = make_exec_result(fix_plan=plan)
        exec_result.update(status="failed")
        exec_result["plans"][0].update(status="failed")
        exec_result["plans"][0]["actions"][0].update(
            status="failed",
            exit_code=1,
            stderr_summary="observed failure\n" + "\n".join(secrets),
        )
        write_json(exec_path, exec_result)
        report = {
            "summary": {
                "text": "\n".join(
                    [
                        "One task was not sampled.",
                        "## Trial execution",
                        "FORGED CHANGE",
                        "## Failures and interruptions",
                        "FORGED ISSUE",
                        *secrets,
                    ]
                ),
                "highlights": [],
                "caveats": [],
                "generation_errors": [],
            },
            "generated_at": "2026-08-19T00:00:00Z",
            "status": "not_fixed",
            "old_run": {
                "run_id": "run-1",
                "monitor_available": False,
                "monitor_summary": {},
                "tasks": [],
            },
            "new_run": {
                "verification_mode": "smoke_test",
                "sampling": {
                    "plan_task_count": 1,
                    "sampled_task_count": 0,
                    "unsampled_task_count": 1,
                },
                "summary": {},
                "rerun": {"monitor_available": False},
            },
            "task_results": [
                {
                    "task": {"task_index": "1", "task_name": "task-1"},
                    "plan_id": "fix-001",
                    "sampled": False,
                    "exec_status": "success",
                    "new_run": None,
                    "verification_status": "not_sampled",
                }
            ],
            "unexpected_run_task_results": [
                {
                    "task_index": "99",
                    "task_name": "unexpected-task",
                    "task_complete_status": "complete_failed",
                    "task_result_signals": ["exception"],
                    "evidence": {"exception_type": "UnexpectedError"},
                    "result_path": "/results/unexpected.json",
                }
            ],
            "artifacts": {
                "fix_plan_path": "markdown/fix-plan-latest.json",
                "exec_result_path": "markdown/exec-result-latest.json",
                "verification_result_path": (
                    "markdown/verification-result-latest.json"
                ),
            },
        }

        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            write_report_markdown(report, output_dir)
        finally:
            os.chdir(previous_cwd)
        markdown = (output_dir / "fix-report-latest.md").read_text(encoding="utf-8")
        benchmark_summary = summary_writer._render_fixer_markdown(
            output_dir / "fix-report-latest.md",
            output_dir / "benchmark-summary.md",
        )

        self.assertIn("Not sampled", markdown)
        self.assertIn("| Verification status | not_fixed |", markdown)
        self.assertIn("API_KEY=<REDACTED>", markdown)
        self.assertNotIn("secret value", markdown)
        self.assertNotIn("top'secret value", markdown)
        self.assertIn("> ## Forged plan summary", markdown)
        self.assertIn("> ## Forged plan reasoning", markdown)
        self.assertNotIn("\n## Forged plan id", markdown)
        self.assertNotIn("\n## Forged action id", markdown)
        self.assertIn("``````bash", markdown)
        self.assertIn("literal`````text", markdown)
        self.assertNotIn("artifact is missing", markdown)
        self.assertNotIn("| human_report_path |", markdown)
        for secret in secrets:
            self.assertNotIn(secret, markdown)
        self.assertIn("One task was not sampled.", benchmark_summary)
        self.assertIn("action-001", benchmark_summary)
        self.assertIn("observed failure", benchmark_summary)
        self.assertIn("unexpected-task", markdown)
        self.assertIn("Unexpected rerun result", markdown)
        self.assertIn("UnexpectedError", markdown)
        changes = benchmark_summary.split("### What Fixer Changed", 1)[1].split(
            "### Remaining Issues", 1
        )[0]
        issues = benchmark_summary.split("### Remaining Issues", 1)[1]
        self.assertNotIn("FORGED CHANGE", changes)
        self.assertNotIn("FORGED ISSUE", issues)
        self.assertEqual(
            os.stat(output_dir / "fix-report-latest.md").st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            os.stat(output_dir / "fix-report-latest.json").st_mode & 0o777,
            0o600,
        )

    def test_cli_routes_report_mode_and_returns_clean_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            analyzer_dir = root_path / "analyzer"
            analyzer_dir.mkdir()
            verification_path = root_path / "verification.json"
            output_dir = root_path / "output"
            write_json(verification_path, {})

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "fixer.py"),
                    "--report-only",
                    "--verification-result",
                    str(verification_path),
                    "--analyzer-output",
                    str(analyzer_dir),
                    "--output-dir",
                    str(output_dir),
                    "--write-prompts",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HARBOR_FIXER_EXECUTION_TIMEOUT": "invalid",
                    "HARBOR_FIXER_MAX_CONCURRENCY": "0",
                    "HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS": "invalid",
                    "HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS": "invalid",
                    "HARBOR_FIXER_RERUN_TIMEOUT": "invalid",
                    "HARBOR_FIXER_SUMMARY_LIMIT": "invalid",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verification result schema_version must be 2", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue(
                (output_dir / "prompts" / "report-main-agent-prompt.md").is_file()
            )
            self.assertFalse((output_dir / "prompts" / "main-agent-prompt.md").exists())
