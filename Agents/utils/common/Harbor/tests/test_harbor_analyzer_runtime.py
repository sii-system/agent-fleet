#!/usr/bin/env python3
"""Tests for Harbor analyzer runtime artifact and handover helpers."""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from Agents.utils.common.Harbor.scripts.analyzer_subagent import (
    FOLLOW_MAX_FAILURE_ATTEMPTS,
    _default_model,
    _pending_handovers,
    _record_follow_failure,
)
from Agents.utils.common.Harbor.scripts.harbor_analyzer import runner as analyzer_runner
from Agents.utils.common.Harbor.scripts.harbor_analyzer.runner import (
    AnalyzerConfig,
    _task_allowed_paths,
    _task_slug,
    _write_outputs,
    run_handover,
)
from Agents.utils.common.Harbor.scripts.harbor_analyzer.pi import _models_config, dispatch_to_child
from Agents.utils.common.Harbor.scripts.harbor_monitor.contracts import build_analyzer_handover


HANDOVER_ID = "sha256-" + ("b" * 64)
RUN_ID = "run-1"


def task(task_index: str = "1", attempt_id: str | None = None) -> dict[str, object]:
    return {
        "task_index": task_index,
        "task_name": "demo-task",
        "attempt_id": attempt_id,
        "task_complete_status": "complete_failed",
        "task_result_signals": ["result_missing"],
        "result_path": "jobs/demo/job.log",
    }


def monitor_output(attempt_id: str) -> dict[str, object]:
    return {
        "timestamp": "2026-07-20T00:00:00+00:00",
        "task_handover": [task(attempt_id=attempt_id)],
        "task_summary": {},
    }


def handover(run_dir: Path, queue_dir: Path) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "task_analysis_handover",
        "audience": "analyzer_subagent",
        "handover_id": HANDOVER_ID,
        "run_id": RUN_ID,
        "should_run_analyzer": True,
        "analyze_statuses": ["complete_failed", "complete_unknown", "not_complete"],
        "skip_statuses": ["complete_success"],
        "signal_definitions": {
            "result_missing": "The task result is missing.",
        },
        "paths": {
            "run_dir": str(run_dir),
            "queue_dir": str(queue_dir),
        },
        "tasks": [task()],
    }


def task_analysis(handover_id: str, run_id: str, handover_task: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "harbor_task_root_cause_analysis",
        "handover_id": handover_id,
        "run_id": run_id,
        "task": {
            "task_index": handover_task["task_index"],
            "task_name": handover_task["task_name"],
            "attempt_id": handover_task["attempt_id"],
        },
        "analysis_status": "analysis_complete",
        "final_class": "model_fail",
        "failure_stage": "agent_execution",
        "root_cause_code": "model_output_incorrect",
        "root_cause_summary": "The model output was incorrect.",
        "scope": "task",
        "confidence": 0.8,
        "observations": [
            {
                "path": str(handover_task["result_path"]),
                "line_start": 1,
                "line_end": 1,
                "fact": "The expected answer was not produced.",
            }
        ],
        "reasoning_summary": "The task setup and verifier path are normal, but the answer is wrong.",
        "alternatives_considered": [],
        "recommended_events": ["notify_user"],
        "fix_references": [],
    }


def final_json(marker: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "kind": "harbor_analyzer_final",
        "handover_id": HANDOVER_ID,
        "run_id": RUN_ID,
        "benchmark_report": {
            "schema_version": 2,
            "kind": "harbor_benchmark_root_cause_report",
            "handover_id": HANDOVER_ID,
            "run_id": RUN_ID,
            "tasks": [],
        },
        "env_infra_tasks": {
            "schema_version": 2,
            "kind": "harbor_env_infra_task_list",
            "handover_id": HANDOVER_ID,
            "tasks": [],
        },
        "fix_line_index": [{"marker": marker}] if marker else [],
    }
    if marker:
        payload["benchmark_report"]["publication_marker"] = marker
        payload["env_infra_tasks"]["publication_marker"] = marker
    return payload


def write_outputs_process(root: str, marker: str) -> None:
    output_dir = Path(root) / "analyzer"
    config = AnalyzerConfig(
        run_dir=Path(root) / "run",
        queue_dir=Path(root) / "run" / "queue",
        output_dir=output_dir,
    )
    _write_outputs(
        config=config,
        handover_id=HANDOVER_ID,
        final_json=final_json(marker),
        provenance=None,
        prompt_path=None,
        raw_final_json_path=output_dir / "analyzer-final-json" / f"{HANDOVER_ID}.json",
    )


class HarborAnalyzerRuntimeTest(unittest.TestCase):
    def test_analyzer_model_has_no_glm_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_default_model(), "")
            self.assertEqual(
                AnalyzerConfig(
                    run_dir=Path("/tmp/run"),
                    queue_dir=None,
                    output_dir=Path("/tmp/out"),
                ).model,
                "",
            )

    def test_dispatch_requires_configured_model(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = dispatch_to_child(
                prompt="{}",
                analysis_id="sha256-" + ("1" * 64),
                output_dir=Path(root),
                pi_bin=sys.executable,
                provider="harbor-analyzer",
                model="",
                base_url="https://example.test/v1",
                api_key_env="HARBOR_ANALYZER_API_KEY",
                agent_name="harbor_analyzer_pi_subagent",
                timeout_seconds=1,
            )

        self.assertIsNone(result.report)
        self.assertEqual(result.block_reason, "pi_model_not_configured")

    def test_models_config_uses_bearer_auth_header_for_custom_provider(self) -> None:
        config = _models_config(
            provider="harbor-analyzer",
            model="glm-5.2-fp8",
            base_url="https://example.test/v1",
            api_key_env="HARBOR_ANALYZER_API_KEY",
        )

        provider = config["providers"]["harbor-analyzer"]
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$HARBOR_ANALYZER_API_KEY")
        self.assertTrue(provider["authHeader"])

    def test_task_slug_uses_safe_basename_for_untrusted_task_index(self) -> None:
        slug = _task_slug(task(task_index="/tmp/pr83-escape"))

        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)
        self.assertFalse(Path(slug).is_absolute())

    def test_task_allowed_paths_keep_result_path_inside_evidence_roots(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            config = AnalyzerConfig(run_dir=run_dir, queue_dir=queue_dir, output_dir=Path(root) / "out")

            allowed = _task_allowed_paths(
                task(),
                handover_path=run_dir / "handover.json",
                config=config,
            )
            escaped = _task_allowed_paths(
                task(task_index="2") | {"result_path": "/etc/passwd"},
                handover_path=run_dir / "handover.json",
                config=config,
            )

            self.assertIn((run_dir / "jobs/demo/job.log").resolve(), allowed)
            self.assertNotIn(Path("/etc/passwd"), escaped)
            self.assertNotIn(Path("/etc"), escaped)

    def test_handover_id_distinguishes_distinct_attempt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"

            first = build_analyzer_handover(monitor_output("attempt-1"), run_dir=run_dir, queue_dir=queue_dir)
            second = build_analyzer_handover(monitor_output("attempt-2"), run_dir=run_dir, queue_dir=queue_dir)

            self.assertNotEqual(first["tasks"][0]["terminal_fingerprint"], second["tasks"][0]["terminal_fingerprint"])
            self.assertNotEqual(first["handover_id"], second["handover_id"])

    def test_pending_handovers_do_not_deduplicate_distinct_attempts_by_handover_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            handoff_dir = Path(root) / "handoffs"
            handoff_dir.mkdir()
            for index, attempt_id in enumerate(("attempt-1", "attempt-2"), start=1):
                payload = {
                    "handover_id": HANDOVER_ID,
                    "generated_at": f"2026-07-20T00:00:0{index}+00:00",
                    "tasks": [task(attempt_id=attempt_id)],
                }
                (handoff_dir / f"{index}.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

            pending = _pending_handovers(
                latest_path=Path(root) / "latest.json",
                handoff_dir=handoff_dir,
                processed=set(),
                failed={},
                now=0.0,
            )

            self.assertEqual(
                [item[0]["tasks"][0]["attempt_id"] for item in pending],
                ["attempt-1", "attempt-2"],
            )
            self.assertEqual(len({item[2] for item in pending}), 2)

    def test_pending_handovers_stop_after_follow_failure_limit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            handoff_dir = Path(root) / "handoffs"
            handoff_dir.mkdir()
            payload = {
                "handover_id": HANDOVER_ID,
                "generated_at": "2026-07-20T00:00:00+00:00",
                "tasks": [task()],
            }
            handover_path = handoff_dir / "1.json"
            handover_path.write_text(json.dumps(payload), encoding="utf-8")
            failed: dict[str, dict[str, object]] = {}
            pending = _pending_handovers(
                latest_path=Path(root) / "latest.json",
                handoff_dir=handoff_dir,
                processed=set(),
                failed=failed,
                now=0.0,
            )
            follow_key = pending[0][2]

            for _ in range(FOLLOW_MAX_FAILURE_ATTEMPTS):
                _record_follow_failure(
                    failed,
                    handover_id=follow_key,
                    exit_code=2,
                    poll_interval=1.0,
                )

            self.assertTrue(failed[follow_key]["retry_exhausted"])
            self.assertEqual(
                _pending_handovers(
                    latest_path=Path(root) / "latest.json",
                    handoff_dir=handoff_dir,
                    processed=set(),
                    failed=failed,
                    now=999999.0,
                ),
                [],
            )

    def test_run_handover_keeps_task_evidence_paths_per_publication(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run"
            queue_dir = run_dir / "queue"
            output_dir = Path(root) / "analyzer"
            payload = handover(run_dir, queue_dir)
            source_path = run_dir / "handover.json"
            config = AnalyzerConfig(run_dir=run_dir, queue_dir=queue_dir, output_dir=output_dir)

            def fake_dispatch(**kwargs):
                report = task_analysis(HANDOVER_ID, RUN_ID, payload["tasks"][0])
                analysis_id = kwargs["analysis_id"]
                dispatch_output_dir = kwargs["output_dir"]
                return SimpleNamespace(
                    report=report,
                    block_reason=None,
                    provenance={
                        "analysis_id": analysis_id,
                        "events_path": str(dispatch_output_dir / "analyzer-subagent-events" / f"{analysis_id}.jsonl"),
                        "stderr_path": str(dispatch_output_dir / "analyzer-subagent-stderr" / f"{analysis_id}.txt"),
                        "tool_access_audit_path": str(dispatch_output_dir / "analyzer-tool-access" / f"{analysis_id}.jsonl"),
                    },
                )

            with mock.patch.object(analyzer_runner, "dispatch_to_child", side_effect=fake_dispatch):
                first, first_exit = run_handover(payload, handover_path=source_path, config=config)
                second, second_exit = run_handover(payload, handover_path=source_path, config=config)

            self.assertEqual(first_exit, 0)
            self.assertEqual(second_exit, 0)
            first_pub = first["analyzer_metadata"]["publication_id"]
            second_pub = second["analyzer_metadata"]["publication_id"]
            self.assertNotEqual(first_pub, second_pub)
            task_slug = _task_slug(task())

            for report, publication_id in ((first, first_pub), (second, second_pub)):
                provenance = report["analyzer_metadata"]["agent_provenance"]["per_task"][task_slug]
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}/", provenance["prompt_path"])
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}/", provenance["raw_task_json_path"])
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}/", provenance["events_path"])
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}/", provenance["stderr_path"])
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}/", provenance["tool_access_audit_path"])

    def test_write_outputs_publishes_one_latest_artifact_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "analyzer"
            config = AnalyzerConfig(
                run_dir=Path(root) / "run",
                queue_dir=Path(root) / "run" / "queue",
                output_dir=output_dir,
            )

            _write_outputs(
                config=config,
                handover_id=HANDOVER_ID,
                final_json=final_json(),
                provenance=None,
                prompt_path=None,
                raw_final_json_path=output_dir / "analyzer-final-json" / f"{HANDOVER_ID}.json",
            )

            manifest_path = output_dir / "analyzer-artifacts-latest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["handover_id"], HANDOVER_ID)
            publication_id = manifest["publication_id"]
            self.assertEqual(
                manifest["artifacts"]["benchmark_report_path"],
                str(output_dir / "analyzer-runs" / HANDOVER_ID / f"{publication_id}.json"),
            )
            self.assertEqual(
                manifest["artifacts"]["env_infra_tasks_path"],
                str(output_dir / "env-infra-tasks" / HANDOVER_ID / f"{publication_id}.json"),
            )
            self.assertEqual(
                manifest["artifacts"]["fix_line_index_path"],
                str(output_dir / "fix-line-index" / HANDOVER_ID / f"{publication_id}.jsonl"),
            )
            self.assertEqual(
                manifest["artifacts"]["raw_final_json_path"],
                str(output_dir / "analyzer-final-json" / HANDOVER_ID / f"{publication_id}.json"),
            )

    def test_latest_manifest_uses_one_immutable_publication_for_same_handover_writers(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            processes = [
                multiprocessing.Process(target=write_outputs_process, args=(root, marker))
                for marker in ("first", "second")
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            output_dir = Path(root) / "analyzer"
            manifest = json.loads(
                (output_dir / "analyzer-artifacts-latest.json").read_text(encoding="utf-8")
            )
            publication_id = manifest["publication_id"]
            artifacts = manifest["artifacts"]

            for path in artifacts.values():
                self.assertIsNotNone(path)
                self.assertIn(f"/{HANDOVER_ID}/{publication_id}", path)

            report = json.loads(Path(artifacts["benchmark_report_path"]).read_text(encoding="utf-8"))
            env_infra = json.loads(Path(artifacts["env_infra_tasks_path"]).read_text(encoding="utf-8"))
            raw_final = json.loads(Path(artifacts["raw_final_json_path"]).read_text(encoding="utf-8"))
            fix_line = Path(artifacts["fix_line_index_path"]).read_text(encoding="utf-8").strip()
            fix_record = json.loads(fix_line)

            marker = report["publication_marker"]
            self.assertEqual(env_infra["publication_marker"], marker)
            self.assertEqual(raw_final["benchmark_report"]["publication_marker"], marker)
            self.assertEqual(raw_final["env_infra_tasks"]["publication_marker"], marker)
            self.assertEqual(fix_record["marker"], marker)


if __name__ == "__main__":
    unittest.main()
