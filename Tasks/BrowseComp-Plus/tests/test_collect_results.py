from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from collect_results import collect, collect_trial, discover_results  # noqa: E402


class CollectResultsTest(unittest.TestCase):
    def test_collects_pi_answer_and_extension_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            trial = Path(root) / "q1__abc"
            agent = trial / "agent"
            agent.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({"task_name": "q1", "trial_name": "trial-1", "agent_result": {}}),
                encoding="utf-8",
            )
            message = {
                "type": "message_end",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Final answer [42]\nConfidence: 80%"}]},
            }
            (agent / "pi.txt").write_text(json.dumps(message) + "\n", encoding="utf-8")
            (agent / "browsecomp-events.jsonl").write_text(
                json.dumps({"tool": "search", "docids": ["42", "43"]}) + "\n" + json.dumps({"tool": "get_document", "docids": ["42"]}) + "\n",
                encoding="utf-8",
            )

            item = collect_trial(trial / "result.json")
            self.assertEqual(item["query_id"], "q1")
            self.assertEqual(item["status"], "completed")
            self.assertEqual(item["tool_call_counts"], {"search": 1, "get_document": 1})
            self.assertEqual(item["retrieved_docids"], ["42", "43"])
            self.assertIn("Final answer", item["result"][0]["output"])
            self.assertEqual(discover_results(Path(root))["q1"], trial / "result.json")

    def test_collects_native_mcp_calls_from_opencode_json_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            trial = Path(root) / "q2__abc"
            agent = trial / "agent"
            agent.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({"task_name": "q2", "exception_info": None}), encoding="utf-8"
            )
            events = [
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "mcp__browsecomp__search",
                        "callID": "call-1",
                        "state": {"output": '[{"docid":"doc-7","snippet":"evidence"}]'},
                    },
                },
                {"type": "text", "part": {"text": "Native final answer [doc-7]"}},
            ]
            (agent / "opencode.txt").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )

            item = collect_trial(trial / "result.json")
            self.assertEqual(item["tool_call_counts"], {"search": 1})
            self.assertEqual(item["retrieved_docids"], ["doc-7"])
            self.assertEqual(item["result"][0]["output"], "Native final answer [doc-7]")

    def test_uses_post_compaction_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            trial = Path(root) / "q-compact__abc"
            agent = trial / "agent"
            agent.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({"task_name": "q-compact", "exception_info": None}),
                encoding="utf-8",
            )
            events = [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Still searching"}],
                    },
                },
                {
                    "type": "compaction_end",
                    "reason": "threshold",
                    "result": {"summary": "Research remains unfinished"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Explanation: evidence [42]\nExact Answer: Example\nConfidence: 90%",
                            }
                        ],
                    },
                },
            ]
            (agent / "pi.txt").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            item = collect_trial(trial / "result.json")

            self.assertEqual(item["status"], "completed")
            self.assertIn("Exact Answer: Example", item["result"][0]["output"])
            self.assertNotIn("Still searching", item["result"][0]["output"])

    def test_accepts_harbor_null_agent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            trial = Path(root) / "q3__abc"
            agent = trial / "agent"
            agent.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": "q3",
                        "agent_info": {
                            "name": "pi",
                            "model_info": {"name": "fixture-model"},
                        },
                        "agent_result": {"metadata": None},
                        "exception_info": None,
                    }
                ),
                encoding="utf-8",
            )
            (agent / "pi.txt").write_text(
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Exact Answer: fixture"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            item = collect_trial(trial / "result.json")
            self.assertEqual(item["status"], "completed")
            self.assertEqual(item["metadata"]["agent"], "pi")
            self.assertEqual(item["metadata"]["model"], "fixture-model")

    def test_manifest_reconciliation_counts_missing_and_setup_failed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            trial = root_path / "jobs" / "q1__setup-failed"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({"task_name": "q1", "exception_info": {"type": "SetupError"}}),
                encoding="utf-8",
            )
            manifest = root_path / "task-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "task_count": 2,
                        "tasks": [{"query_id": "q1"}, {"query_id": "q2"}],
                    }
                ),
                encoding="utf-8",
            )
            output = root_path / "official"
            output.mkdir()
            (output / "stale.json").write_text("{}", encoding="utf-8")

            items = collect(root_path / "jobs", output, task_manifest=manifest)

            self.assertEqual([item["query_id"] for item in items], ["q1", "q2"])
            self.assertEqual([item["status"] for item in items], ["failed", "failed"])
            self.assertIn("harbor_result", items[0]["metadata"])
            self.assertEqual(
                items[1]["metadata"]["collection_error"],
                "missing Harbor result.json",
            )
            self.assertFalse((output / "stale.json").exists())
            self.assertEqual(
                sorted(path.name for path in output.glob("*.json")),
                ["q1.json", "q2.json"],
            )


if __name__ == "__main__":
    unittest.main()
