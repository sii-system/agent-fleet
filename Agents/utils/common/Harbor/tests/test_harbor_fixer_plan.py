"""Tests for Harbor Fixer plan."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TEST_DIR.parent / "scripts"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fixer_test_support import (
    FixerTestCase,
    MainInvoker,
    SequenceInvoker,
    SummaryInvoker,
    task_summary_for,
    write_analyzer_fixture,
    write_fixture_pi,
)
from harbor_fixer import plan_generation
from harbor_fixer.agent_invocation import PiAgentInvoker, PiInvocationConfig
from harbor_fixer.analyzer_inputs import build_task_inputs
from harbor_fixer.artifact_io import write_json_atomic
from harbor_fixer.plan_generation import (
    collect_task_summaries,
    request_fix_plan,
    run_plan_generation,
)
from harbor_fixer.validation import (
    ValidationError,
    parse_strict_json_object,
    validate_fix_plan_set,
    validate_task_summary,
)
from harbor_pi_runtime import PiInvocationError


class HarborFixerPlanTest(FixerTestCase):
    def test_permanent_provider_errors_stop_task_and_main_plan_retries(self):
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        invoker = mock.Mock()
        invoker.invoke.side_effect = PiInvocationError(
            "pi_provider_request_failed:404_model_not_found_fake_provider_credential", label="fixture", attempt=1
        )
        with mock.patch.object(plan_generation, "sleep_before_retry") as backoff:
            summaries, errors = collect_task_summaries([task_input], invoker, self.root / "tasks")
            self.assertEqual(summaries, [])
            self.assertIn("pi_provider_request_failed:404", errors[0]["error"])
            self.assertNotIn("fake_provider_credential", json.dumps(errors))
            self.assertEqual(invoker.invoke.call_count, 1)
            with self.assertRaisesRegex(ValidationError, "after 1 attempts") as caught:
                request_fix_plan(invoker, {}, self.root / "main")
            self.assertNotIn("fake_provider_credential", str(caught.exception))
            self.assertEqual(invoker.invoke.call_count, 2)
            backoff.assert_not_called()

    def test_fixer_prompt_is_compact_and_oversized_input_never_launches_pi(self):
        from harbor_fixer.agent_invocation import FIXER_INPUT_MARKER, _compose_prompt

        payload = {"evidence": [{"fact": "unicode 界 and a newline\n"}] * 100}
        prompt = _compose_prompt("instructions", payload)
        self.assertEqual(json.loads(prompt.split(FIXER_INPUT_MARKER)[1]), payload)
        self.assertLess(len(prompt), len(json.dumps(payload, ensure_ascii=False, indent=2)))
        output_dir = self.root / "oversized"
        invoker = PiAgentInvoker(output_dir, PiInvocationConfig(max_prompt_bytes=100))
        with (
            mock.patch("harbor_fixer.agent_invocation.run_pi_json_process") as process,
            self.assertRaises(PiInvocationError) as caught,
        ):
            invoker.invoke("instructions", payload, attempt=1, label="task")
        self.assertEqual(caught.exception.block_reason, "pi_prompt_too_large")
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(output_dir.exists())
        process.assert_not_called()

    def test_fixer_keeps_provider_failure_classification_for_retry_decisions(self):
        from types import SimpleNamespace

        result = SimpleNamespace(
            output_json=None,
            block_reason="pi_provider_request_failed:503_serviceunavailableerror",
            provenance={},
        )
        invoker = PiAgentInvoker(self.root / "provider-failure", PiInvocationConfig())
        with (
            mock.patch("harbor_fixer.agent_invocation.run_pi_json_process", return_value=result),
            self.assertRaises(PiInvocationError) as caught,
        ):
            invoker.invoke("instructions", {}, attempt=1, label="task")
        self.assertEqual(caught.exception.block_reason, "pi_provider_request_failed:503")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(list((self.root / "provider-failure" / "active-agent-processes").glob("*.json")))

    def test_transient_harness_failures_recover_task_summary_and_main_plan(self):
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        summary = task_summary_for(task_input)
        task_invoker = mock.Mock()
        task_invoker.invoke.side_effect = [
            PiInvocationError("pi_dispatch_exit_code:-6", label="task", attempt=1),
            json.dumps(summary),
        ]
        with mock.patch.object(plan_generation, "sleep_before_retry"):
            summaries, errors = collect_task_summaries([task_input], task_invoker, self.root / "tasks")
        self.assertEqual(summaries, [summary])
        self.assertEqual(errors, [])
        main_input = {"source": {"fixture": True}, "task_summaries": summaries, "generation_errors": []}
        valid_plan = MainInvoker().invoke("", main_input, attempt=2, label="main")
        main_invoker = mock.Mock()
        main_invoker.invoke.side_effect = [
            PiInvocationError("pi_jsonl_invalid", label="main", attempt=1), valid_plan,
        ]
        with mock.patch.object(plan_generation, "sleep_before_retry"):
            result = request_fix_plan(main_invoker, main_input, self.root / "main")
        self.assertEqual(result, json.loads(valid_plan))

    def test_task_summary_retry_identity_and_contract(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root, agent="opencode")
        env_monitor_root = self.root / "environment-monitor"
        write_analyzer_fixture(env_monitor_root, agent="claude-code")
        with mock.patch.dict(
            os.environ, {"HARBOR_MONITOR_DIR": str(env_monitor_root / "monitor")}
        ):
            task_inputs, source = build_task_inputs(analyzer_dir)
        task_input = task_inputs[0]
        self.assertEqual(source["agent"], "opencode")
        self.assertEqual(
            source["monitor_path"],
            str(self.root / "monitor" / "monitor-latest.json"),
        )

        invoker = SequenceInvoker(["not-json", json.dumps(task_summary_for(task_input))])
        summaries, errors = collect_task_summaries([task_input], invoker, self.root / "out")
        self.assertEqual((len(summaries), errors), (1, []))
        retry_prompt = invoker.records[1][0]
        self.assertIn("invalid JSON:", retry_prompt)
        self.assertIn("<previous-output>\nnot-json\n</previous-output>", retry_prompt)

        bad_summary = json.loads(json.dumps(task_summary_for(task_input)))
        bad_summary["analyzer_alignment"]["final_class"] = "infra_fail"
        with self.assertRaises(ValidationError):
            validate_task_summary(bad_summary, expected_input=task_input)

        monitor_path = Path(source["monitor_path"])
        monitor = json.loads(monitor_path.read_text(encoding="utf-8"))
        monitor["analyzer_handover"]["run_id"] = "other-run"
        monitor_path.write_text(json.dumps(monitor), encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"HARBOR_MONITOR_DIR": str(env_monitor_root / "monitor")}
        ):
            _, source = build_task_inputs(analyzer_dir)
        self.assertEqual(source["agent"], "claude-code")
        self.assertEqual(
            source["monitor_path"],
            str(env_monitor_root / "monitor" / "monitor-latest.json"),
        )

        fallback_root = self.root / "fallback-monitor"
        fallback_analyzer = write_analyzer_fixture(fallback_root, agent="opencode")
        fallback_manifest_path = fallback_analyzer / "analyzer-artifacts-latest.json"
        fallback_manifest = json.loads(fallback_manifest_path.read_text(encoding="utf-8"))
        fallback_manifest["monitor_path"] = "/missing/monitor-latest.json"
        fallback_manifest_path.write_text(json.dumps(fallback_manifest), encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"HARBOR_MONITOR_DIR": str(fallback_root / "monitor")}
        ):
            _, source = build_task_inputs(fallback_analyzer)
        self.assertEqual(source["agent"], "opencode")

        analyzer_without_monitor = write_analyzer_fixture(self.root / "no-monitor")
        (analyzer_without_monitor.parent / "monitor" / "monitor-latest.json").unlink()
        with mock.patch.dict(os.environ, {"HARBOR_MONITOR_DIR": ""}):
            _, source = build_task_inputs(analyzer_without_monitor)
        self.assertEqual(source["agent"], "")
        self.assertEqual(source["monitor_path"], "")

    def test_analyzer_output_uses_environment_when_cli_is_omitted(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root)
        output_dir = self.root / "prepared"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--prepare-only",
                "--output-dir",
                str(output_dir),
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "HARBOR_ANALYZER_OUTPUT_DIR": str(analyzer_dir),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        source = json.loads((output_dir / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["analyzer_root"], str(analyzer_dir.resolve()))

    def test_main_plan_contract_and_generation_error_ownership(self) -> None:
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        summary = task_summary_for(task_input)
        main_input = {
            "source": {"run_id": "fixture"},
            "task_summaries": [summary],
            "generation_errors": [{"stage": "task_subagent", "error": "fixture"}],
        }
        valid_output = MainInvoker().invoke(
            "",
            main_input,
            attempt=1,
            label="main-agent",
        )
        plan = request_fix_plan(
            SequenceInvoker([valid_output]),
            main_input,
            self.root / "main-out",
        )
        self.assertEqual(plan["generation_errors"], main_input["generation_errors"])

        incomplete = json.loads(valid_output)
        del incomplete["plans"][0]["actions"][0]["expected_effect"]
        with self.assertRaisesRegex(ValidationError, "expected_effect"):
            validate_fix_plan_set(
                incomplete,
                expected_source=main_input["source"],
                expected_task_summaries=[summary],
            )

        with_file_edit = json.loads(valid_output)
        with_file_edit["plans"][0]["actions"].append(
            {
                "action_id": "action-002",
                "action_type": "file_edit",
                "cwd": ".",
                "path": "config.json",
                "edit": {
                    "kind": "replace_text",
                    "old_text": '"enabled": false',
                    "new_text": '"enabled": true',
                    "expected_replacements": 1,
                },
                "purpose": "Enable the fixture.",
                "expected_effect": "The fixture is enabled.",
            }
        )
        validate_fix_plan_set(with_file_edit, expected_task_summaries=[summary])

        raw_command = json.loads(valid_output)
        raw_command["plans"][0]["actions"][0]["command"] = "printf hello"
        with self.assertRaisesRegex(ValidationError, "unexpected command"):
            validate_fix_plan_set(raw_command, expected_task_summaries=[summary])

        legacy_commands = json.loads(valid_output)
        legacy_commands["plans"][0]["commands"] = legacy_commands["plans"][0].pop(
            "actions"
        )
        with self.assertRaisesRegex(ValidationError, "missing actions"):
            validate_fix_plan_set(legacy_commands, expected_task_summaries=[summary])

        invalid_edit = json.loads(json.dumps(with_file_edit))
        invalid_edit["plans"][0]["actions"][1]["edit"]["expected_replacements"] = 0
        with self.assertRaisesRegex(ValidationError, "must be positive"):
            validate_fix_plan_set(invalid_edit, expected_task_summaries=[summary])

        no_op_edit = json.loads(json.dumps(with_file_edit))
        edit = no_op_edit["plans"][0]["actions"][1]["edit"]
        edit["new_text"] = edit["old_text"]
        with self.assertRaisesRegex(ValidationError, "must change"):
            validate_fix_plan_set(no_op_edit, expected_task_summaries=[summary])

        agent_owned_errors = json.loads(valid_output)
        agent_owned_errors["generation_errors"] = [{"stage": "invented"}]
        with self.assertRaisesRegex(ValidationError, "generation_errors must be empty"):
            request_fix_plan(
                SequenceInvoker([json.dumps(agent_owned_errors)]),
                main_input,
                self.root / "bad-main-out",
                max_attempts=1,
            )

        for comparison, error in (
            ({"analyzer_scopes": ["host"]}, "analyzer scopes"),
            ({"relation": "broader"}, "scope relation"),
        ):
            invalid = json.loads(valid_output)
            invalid["plans"][0]["analyzer_scope_comparison"].update(comparison)
            with self.assertRaisesRegex(ValidationError, error):
                validate_fix_plan_set(invalid, expected_task_summaries=[summary])

        invalid = json.loads(valid_output)
        invalid["plans"][0]["task_list"][0]["attempt_id"] = ""
        with self.assertRaisesRegex(ValidationError, "identity"):
            validate_fix_plan_set(invalid, expected_task_summaries=[summary])

        for value in ("NaN", "Infinity", "1e400"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError,
                "invalid JSON",
            ):
                parse_strict_json_object(f'{{"value": {value}}}')

    def test_summary_size_limits(self) -> None:
        task_input = build_task_inputs(write_analyzer_fixture(self.root))[0][0]
        output = json.dumps(task_summary_for(task_input))

        for limits, error in (
            ({"max_task_summary_chars": 1}, "task_summary_exceeds_size_limit"),
            (
                {"max_task_summaries_chars": 1},
                "task_summaries_exceed_aggregate_size_limit",
            ),
        ):
            summaries, errors = collect_task_summaries(
                [task_input],
                SequenceInvoker([output]),
                self.root / error,
                **limits,
            )
            self.assertEqual((summaries, errors[0]["error"]), ([], error))

    def test_empty_analyzer_output_writes_plan_without_agents(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root, count=0)
        task_invoker = SequenceInvoker([])
        main_invoker = SequenceInvoker([])

        plan = run_plan_generation(
            analyzer_dir,
            self.root / "empty-plan",
            task_invoker,
            main_invoker,
            workspace_root=self.root,
        )

        self.assertEqual(plan["plans"], [])
        self.assertEqual(plan["unplanned_tasks"], [])
        self.assertEqual((task_invoker.calls, main_invoker.calls), (0, 0))
        validate_fix_plan_set(
            plan,
            expected_source=plan["source"],
            expected_task_summaries=[],
        )

    def test_all_task_failures_write_diagnostics_and_fail(self) -> None:
        output_dir = self.root / "failed-plan"
        main_invoker = SequenceInvoker([])

        with self.assertRaisesRegex(ValidationError, "all task subagents failed"):
            run_plan_generation(
                write_analyzer_fixture(self.root),
                output_dir,
                SequenceInvoker(["not-json"]),
                main_invoker,
                workspace_root=self.root,
            )

        plan = json.loads((output_dir / "fix-plan-latest.json").read_text())
        self.assertEqual(len(plan["generation_errors"]), 1)
        self.assertEqual(main_invoker.calls, 0)

    def test_latest_plan_publish_and_failed_regeneration(self) -> None:
        output_dir = self.root / "regeneration"
        latest = output_dir / "fix-plan-latest.json"
        latest.parent.mkdir()
        latest.write_text('{"stale": true}')

        with (
            mock.patch.object(Path, "replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            write_json_atomic(latest, {"stale": False})
        self.assertEqual(json.loads(latest.read_text()), {"stale": True})

        with (
            mock.patch.object(
                plan_generation,
                "request_fix_plan",
                side_effect=ValidationError("main agent failed"),
            ),
            self.assertRaises(ValidationError),
        ):
            run_plan_generation(
                write_analyzer_fixture(self.root),
                output_dir,
                SummaryInvoker(),
                SequenceInvoker([]),
                workspace_root=self.root,
            )

        self.assertFalse(latest.exists())

    def test_write_json_atomic_does_not_follow_preexisting_temp_symlink(self) -> None:
        output = self.root / "atomic-output" / "exec-result-latest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside.json"
        outside.write_text("outside\n", encoding="utf-8")
        temp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        temp.symlink_to(outside)
        try:
            write_json_atomic(output, {"ok": True})
            self.assertFalse(output.is_symlink())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"ok": True},
            )
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        finally:
            if temp.exists() or temp.is_symlink():
                temp.unlink()

    def test_plan_generation_and_cli_smoke_write_fix_plan(self) -> None:
        analyzer_dir = write_analyzer_fixture(self.root)
        cli_out = self.root / "cli-fixer"
        api_key = "fixture-super-secret-api-key"
        pi_bin = write_fixture_pi(self.root / "fixture_pi.py")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "fixer.py"),
                "--analyzer-output",
                str(analyzer_dir),
                "--output-dir",
                str(cli_out),
                "--timeout",
                "60",
                "--max-task-summary-chars",
                "100000",
                "--workspace-root",
                str(self.root),
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "HARBOR_FIXER_API_KEY": api_key,
                "HARBOR_FIXER_PI_BIN": str(pi_bin),
                "HARBOR_FIXER_BASE_URL": "https://example.test/v1",
                "HARBOR_FIXER_MODEL": "fixture-model",
                "HARBOR_FIXER_AGENT_TIMEOUT": "bad",
                "HARBOR_FIXER_MAX_TASK_SUMMARY_CHARS": "1",
                "HARBOR_FIXER_MAX_TASK_SUMMARIES_CHARS": "200000",
                "HARBOR_ANALYZER_OUTPUT_DIR": str(self.root / "wrong-analyzer"),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(
            (cli_out / "fix-plan-latest.json").read_text(encoding="utf-8")
        )
        validate_fix_plan_set(plan)
        main_input_path = cli_out / "main-agent-input.json"
        main_input = json.loads(main_input_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["source"]["agent"], "claude-code")
        self.assertEqual(plan["source"], main_input["source"])
        for name in ("target_environment_artifact", "target_context_artifact"):
            artifact = main_input[name]
            self.assertEqual(
                artifact["sha256"],
                hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(),
            )
        self.assertNotIn(
            api_key,
            main_input_path.read_text(encoding="utf-8"),
        )
        main_prompt = next(
            (cli_out / "pi-agent-prompts").glob("main-agent/attempt-1.txt")
        )
        self.assertNotIn(api_key, main_prompt.read_text(encoding="utf-8"))
        provenance_paths = sorted(
            (cli_out / "pi-agent-provenance").glob("task-*/attempt-1.json")
        )
        self.assertEqual(len(provenance_paths), 1)
        provenance = json.loads(provenance_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(provenance["thinking_level"], "off")


if __name__ == "__main__":
    unittest.main()
