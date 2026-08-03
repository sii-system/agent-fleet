"""Shared fixtures and test doubles for Harbor Fixer stage tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class FixerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)


def make_task(index: int) -> dict:
    return {
        "task": {"task_index": str(index), "task_name": f"task-{index}", "attempt_id": None},
        "final_class": "env_fail",
        "failure_stage": "environment_setup",
        "scope": "benchmark",
        "confidence": 0.91,
        "root_cause_code": "docker_registry_unavailable",
        "root_cause_summary": "Docker registry is unreachable.",
        "evidence": {
            "path": f"/logs/task-{index}.log",
            "line_start": 10,
            "line_end": 12,
            "fact": "docker pull cannot reach registry",
            "reason": "The environment setup fails before task execution.",
        },
    }


def write_analyzer_fixture(
    root: Path,
    count: int = 1,
    *,
    handover_task_indexes: tuple[tuple[int, ...], ...] | None = None,
) -> Path:
    analyzer_dir = root / "analyzer"
    task_indexes = (
        handover_task_indexes
        if handover_task_indexes is not None
        else (tuple(range(1, count + 1)),)
    )
    publications = []
    for handover_number, indexes in enumerate(task_indexes, start=1):
        handover_id = f"handover-{handover_number}"
        publication_id = (
            "publication-current"
            if handover_number == 1
            else f"publication-current-{handover_number}"
        )
        tasks = [make_task(index) for index in indexes]
        publications.append(
            {
                "handover_id": handover_id,
                "publication_id": publication_id,
                "generated_at": "2026-07-16T00:00:00Z",
                "artifacts": {
                    "env_infra_tasks_path": "/stale-copy/env-infra-tasks.json",
                    "fix_line_index_path": "/stale-copy/fix-line-index.jsonl",
                },
            }
        )
        env_infra_path = (
            analyzer_dir
            / "env-infra-tasks"
            / handover_id
            / f"{publication_id}.json"
        )
        fix_line_index_path = (
            analyzer_dir
            / "fix-line-index"
            / handover_id
            / f"{publication_id}.jsonl"
        )
        write_json(
            env_infra_path,
            {
                "schema_version": 2,
                "kind": "harbor_env_infra_task_list",
                "handover_id": handover_id,
                "generated_at": "2026-07-16T00:00:00Z",
                "task_count": len(tasks),
                "tasks": [
                    {
                        "task": task["task"],
                        "final_class": task["final_class"],
                        "failure_stage": task["failure_stage"],
                        "scope": task["scope"],
                        "confidence": task["confidence"],
                        "root_cause_code": task["root_cause_code"],
                        "root_cause_summary": task["root_cause_summary"],
                    }
                    for task in tasks
                ],
            },
        )
        fix_line_index_path.parent.mkdir(parents=True, exist_ok=True)
        with fix_line_index_path.open("w", encoding="utf-8") as handle:
            for task in tasks:
                ref = dict(task["evidence"])
                ref.update(
                    {
                        "schema_version": 2,
                        "kind": "harbor_fix_line_reference",
                        "task": task["task"],
                        "root_cause_code": task["root_cause_code"],
                    }
                )
                handle.write(json.dumps(ref) + "\n")

    write_json(
        analyzer_dir / "analyzer-artifacts-latest.json",
        {
            "schema_version": 2,
            "kind": "harbor_analyzer_latest_artifacts",
            "handover_id": publications[-1]["handover_id"],
            "publication_id": publications[-1]["publication_id"],
            "run_id": "run-1",
            "generated_at": "2026-07-16T00:00:00Z",
            "artifacts": {
                "env_infra_tasks_path": "/stale-copy/env-infra-tasks.json",
                "fix_line_index_path": "/stale-copy/fix-line-index.jsonl",
            },
            "publications": publications,
        },
    )
    return analyzer_dir


def task_summary_for(task_input: dict) -> dict:
    analyzer = task_input["analyzer_result"]
    evidence = task_input["evidence"][0]
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_task_summary",
        "task": task_input["task"],
        "analyzer_alignment": {
            "final_class": analyzer["final_class"],
            "analyzer_scope": analyzer["scope"],
            "root_cause_code": analyzer["root_cause_code"],
            "scope_agreement": "agree",
        },
        "root_cause_summary": analyzer["root_cause_summary"],
        "reasoning_summary": analyzer["root_cause_summary"],
        "strongest_evidence": [
            {
                "path": evidence["path"],
                "line_start": evidence["line_start"],
                "line_end": evidence["line_end"],
                "summary": evidence["fact"],
            }
        ],
        "fix_direction": {
            "suggested_scope": "benchmark",
            "summary": "Configure benchmark registry mirror.",
            "why_this_should_fix_it": "The failure happens during Docker pull.",
        },
        "grouping_key_hint": analyzer["root_cause_code"],
        "confidence": "high",
        "unknowns": [],
    }


class SequenceInvoker:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.records: list[tuple[str, dict, int, str]] = []

    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        self.records.append((prompt, payload, attempt, label))
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


class SummaryInvoker:
    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        return json.dumps(task_summary_for(payload))


class MainInvoker:
    def invoke(self, prompt: str, payload: dict, *, attempt: int, label: str) -> str:
        summaries = payload["task_summaries"]
        plan = make_fix_plan()
        plan["source"] = payload["source"]
        plan["plans"][0]["task_list"] = [
            {
                "task_index": summary["task"]["task_index"],
                "task_name": summary["task"]["task_name"],
                "attempt_id": summary["task"]["attempt_id"],
                "root_cause_code": summary["analyzer_alignment"]["root_cause_code"],
                "final_class": summary["analyzer_alignment"]["final_class"],
            }
            for summary in summaries
        ]
        plan["plans"][0]["verification_hint"]["target_task_indexes"] = [
            summary["task"]["task_index"] for summary in summaries
        ]
        return json.dumps(plan)


def make_fix_plan() -> dict:
    return {
        "schema_version": 1,
        "kind": "harbor_fixer_fix_plan_set",
        "source": {"fixture": True},
        "plans": [
            {
                "plan_id": "fix-001",
                "fix_scope": "benchmark",
                "analyzer_scope_comparison": {
                    "analyzer_scopes": ["benchmark"],
                    "relation": "same",
                    "reason": "Fixture plan.",
                },
                "task_list": [
                    {
                        "task_index": "1",
                        "task_name": "task-1",
                        "attempt_id": None,
                        "root_cause_code": "fixture",
                        "final_class": "env_fail",
                    }
                ],
                "commands": [
                    {
                        "command_id": "cmd-001",
                        "cwd": ".",
                        "command": "printf '%s\\n' hello",
                        "purpose": "Emit a harmless test line.",
                        "expected_effect": "stdout contains hello.",
                    }
                ],
                "fix_reason": {"summary": "Fixture fix.", "evidence": [], "reasoning": "Fixture reasoning."},
                "verification_hint": {"expected_original_failure_absent": "fixture failure", "target_task_indexes": ["1"]},
            }
        ],
        "unplanned_tasks": [],
        "generation_errors": [],
    }


def write_fixture_pi(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "if '--version' in sys.argv:",
                "    print('fixture-pi 1.0')",
                "    raise SystemExit(0)",
                "raw = sys.stdin.read()",
                "marker = 'HARBOR_FIXER_INPUT_JSON:'",
                "payload = json.loads(raw.split(marker, 1)[1].strip())",
                "if payload['kind'] == 'harbor_fixer_task_input':",
                "    analyzer = payload['analyzer_result']; evidence = payload['evidence'][0]",
                "    result = {",
                "      'schema_version': 1, 'kind': 'harbor_fixer_task_summary', 'task': payload['task'],",
                "      'analyzer_alignment': {'final_class': analyzer['final_class'], 'analyzer_scope': analyzer['scope'], 'root_cause_code': analyzer['root_cause_code'], 'scope_agreement': 'agree'},",
                "      'root_cause_summary': analyzer['root_cause_summary'], 'reasoning_summary': analyzer['root_cause_summary'],",
                "      'strongest_evidence': [{'path': evidence['path'], 'line_start': evidence['line_start'], 'line_end': evidence['line_end'], 'summary': evidence['fact']}],",
                "      'fix_direction': {'suggested_scope': analyzer['scope'], 'summary': analyzer['root_cause_summary'], 'why_this_should_fix_it': 'fixture smoke test'},",
                "      'grouping_key_hint': analyzer['root_cause_code'], 'confidence': 'high', 'unknowns': []}",
                "else:",
                "    summaries = payload['task_summaries']",
                "    result = {",
                "      'schema_version': 1, 'kind': 'harbor_fixer_fix_plan_set', 'source': payload['source'],",
                "      'plans': [{'plan_id': 'fix-001', 'fix_scope': 'benchmark', 'analyzer_scope_comparison': {'analyzer_scopes': ['benchmark'], 'relation': 'same', 'reason': 'fixture'},",
                "        'task_list': [{'task_index': s['task']['task_index'], 'task_name': s['task']['task_name'], 'attempt_id': s['task']['attempt_id'], 'root_cause_code': s['analyzer_alignment']['root_cause_code'], 'final_class': s['analyzer_alignment']['final_class']} for s in summaries],",
                "        'commands': [{'command_id': 'cmd-001', 'cwd': '.', 'command': \"printf '%s\\\\n' fixture-fix\", 'purpose': 'fixture command', 'expected_effect': 'fixture command runs'}],",
                "        'fix_reason': {'summary': 'fixture shared fix', 'evidence': [], 'reasoning': 'fixture'},",
                "        'verification_hint': {'expected_original_failure_absent': 'fixture failure', 'target_task_indexes': [s['task']['task_index'] for s in summaries]}}],",
                "      'unplanned_tasks': [], 'generation_errors': []}",
                "text = '```json\\n' + json.dumps(result) + '\\n```'",
                "events = [",
                "  {'type': 'session', 'id': 'fixture-session'},",
                "  {'type': 'agent_start'},",
                "  {'type': 'turn_start'},",
                "  {'type': 'message_update', 'message': {'role': 'assistant', 'content': 'intermediate-only'}},",
                "  {'type': 'message_end', 'message': {'role': 'assistant', 'content': text, 'stopReason': 'stop'}},",
                "  {'type': 'turn_end', 'message': {'role': 'assistant', 'content': text, 'stopReason': 'stop'}},",
                "  {'type': 'agent_end'},",
                "]",
                "for event in events:",
                "    print(json.dumps(event), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path
