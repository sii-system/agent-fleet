from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from common import load_questions, parse_selection  # noqa: E402
from materialize_tasks import materialize  # noqa: E402


class MaterializeTest(unittest.TestCase):
    def test_materializes_query_only_harbor_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            ground_truth = root_path / "gold.jsonl"
            ground_truth.write_text(
                json.dumps({"query_id": "q-1", "query": "Who won?", "answer": "Secret Winner"}) + "\n",
                encoding="utf-8",
            )
            output = root_path / "tasks"
            task_file = root_path / "tasks.txt"
            manifest = root_path / "manifest.json"
            selected = materialize(
                ground_truth,
                output,
                ["q-1"],
                task_file,
                manifest,
                allowed_hosts=["gateway.example.invalid", "host.docker.internal"],
            )

            self.assertEqual(selected, ["q-1"])
            task_text = "\n".join(path.read_text(errors="ignore") for path in (output / "q-1").rglob("*") if path.is_file())
            self.assertIn("Who won?", task_text)
            self.assertNotIn("Secret Winner", task_text)
            self.assertTrue((output / "q-1" / "task.toml").is_file())
            task_config = (output / "q-1" / "task.toml").read_text(encoding="utf-8")
            self.assertIn('network_mode = "no-network"', task_config)
            self.assertIn('network_mode = "allowlist"', task_config)
            self.assertIn('"gateway.example.invalid"', task_config)
            self.assertIn('"host.docker.internal"', task_config)
            self.assertNotIn("allow_internet", task_config)
            self.assertTrue((output / "q-1" / "tests" / "test.sh").stat().st_mode & 0o111)
            manifest_payload = json.loads(manifest.read_text())
            self.assertFalse(manifest_payload["contains_gold"])
            self.assertEqual(
                manifest_payload["allowed_hosts"],
                ["gateway.example.invalid", "host.docker.internal"],
            )

    def test_rejects_duplicates_unknown_selection_and_unsafe_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "gold.jsonl"
            path.write_text(
                '{"query_id":"../escape","query":"q","answer":"a"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_questions(path, require_answer=True)
            self.assertEqual(parse_selection("q1,q1,,q2"), ["q1", "q2"])

    def test_limit_selects_first_queries_without_requiring_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            ground_truth = root_path / "gold.jsonl"
            ground_truth.write_text(
                "".join(
                    json.dumps({"query_id": f"q{index}", "query": f"Q{index}", "answer": "a"}) + "\n"
                    for index in range(3)
                ),
                encoding="utf-8",
            )
            selected = materialize(
                ground_truth,
                root_path / "tasks",
                [],
                root_path / "tasks.txt",
                root_path / "manifest.json",
                limit=1,
            )
            self.assertEqual(selected, ["q0"])

    def test_rejects_changed_selection_before_overwriting_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            ground_truth = root_path / "gold.jsonl"
            ground_truth.write_text(
                "".join(
                    json.dumps(
                        {"query_id": task_id, "query": task_id, "answer": "a"}
                    )
                    + "\n"
                    for task_id in ("q1", "q2")
                ),
                encoding="utf-8",
            )
            existing = root_path / "harbor-tasks.txt"
            existing.write_text("q1\n", encoding="utf-8")
            manifest = root_path / "manifest.json"
            manifest.write_text('{"sentinel": true}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "set RESET_RUN=1"):
                materialize(
                    ground_truth,
                    root_path / "tasks",
                    ["q2"],
                    root_path / "tasks.txt",
                    manifest,
                    existing_task_file=existing,
                )

            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8")),
                {"sentinel": True},
            )
            self.assertFalse((root_path / "tasks.txt").exists())

            selected = materialize(
                ground_truth,
                root_path / "tasks",
                ["q1"],
                root_path / "tasks.txt",
                manifest,
                existing_task_file=existing,
            )
            self.assertEqual(selected, ["q1"])


if __name__ == "__main__":
    unittest.main()
