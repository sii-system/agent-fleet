from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_template_batch_materialize as batch
import qz_template_manager as manager
import qz_template_mapping as mapping
import qz_template_resolver as resolver


class QzTemplateBatchMaterializeTest(unittest.TestCase):
    def make_mapping(self, root: Path) -> tuple[Path, dict]:
        tasks = []
        for task_name, image in (
            ("task-a", "ubuntu:24.04"),
            ("task-b", "ubuntu:24.04"),
            ("task-c", "debian:12"),
        ):
            task_dir = root / task_name
            task_dir.mkdir()
            (task_dir / "task.toml").write_text(
                f'[environment]\ndocker_image = "{image}"\n',
                encoding="utf-8",
            )
            tasks.append((task_name, task_dir))
        payload = mapping.build_inventory(
            benchmark="terminalbench21",
            tasks=tasks,
        )
        path = root / "mapping.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path, payload

    def test_materializes_unique_templates_concurrently_and_records_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, payload = self.make_mapping(Path(temporary))
            barrier = threading.Barrier(2)
            calls = []
            calls_lock = threading.Lock()
            template_ids = {
                template_key: f"template-{index}"
                for index, template_key in enumerate(payload["templates"], start=1)
            }

            def materialize(template_key, entry, client, *, timeout, stderr):
                del entry, client, timeout, stderr
                with calls_lock:
                    calls.append(template_key)
                barrier.wait(timeout=5)
                return template_ids[template_key]

            with patch.object(
                batch.resolver,
                "materialize_template_entry",
                side_effect=materialize,
            ):
                report = batch.materialize_batch(
                    path,
                    ["task-a", "task-b", "task-a", "task-c"],
                    object(),
                    workers=2,
                    timeout=30,
                    stderr=io.StringIO(),
                )
            saved = resolver.load_mapping(path)

        self.assertCountEqual(calls, payload["templates"])
        self.assertEqual(report["selected_task_count"], 3)
        self.assertEqual(report["unique_template_count"], 2)
        self.assertEqual(report["ready_template_count"], 2)
        self.assertEqual(report["failed_template_count"], 0)
        for template_key, template_id in template_ids.items():
            self.assertEqual(
                saved["templates"][template_key]["template_id"],
                template_id,
            )
        grouped_tasks = sorted(len(result["tasks"]) for result in report["templates"])
        self.assertEqual(grouped_tasks, [1, 2])

    def test_partial_failure_persists_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, payload = self.make_mapping(Path(temporary))
            successful_key = next(
                key
                for key, entry in payload["templates"].items()
                if entry["image"] == "ubuntu:24.04"
            )

            def materialize(template_key, entry, client, *, timeout, stderr):
                del entry, client, timeout, stderr
                if template_key == successful_key:
                    return "template-ready"
                raise manager.QzTemplateError("build failed")

            with patch.object(
                batch.resolver,
                "materialize_template_entry",
                side_effect=materialize,
            ):
                report = batch.materialize_batch(
                    path,
                    ["task-a", "task-b", "task-c"],
                    object(),
                    workers=2,
                    timeout=30,
                    stderr=io.StringIO(),
                )
            saved = resolver.load_mapping(path)

        self.assertEqual(report["ready_template_count"], 1)
        self.assertEqual(report["failed_template_count"], 1)
        self.assertEqual(
            saved["templates"][successful_key]["template_id"],
            "template-ready",
        )
        failed = next(
            result for result in report["templates"] if result["status"] == "failed"
        )
        self.assertIn("build failed", failed["error"])

    def test_main_outputs_json_and_returns_failure_count(self):
        result = {
            "mapping": "/tmp/mapping.json",
            "selected_task_count": 1,
            "unique_template_count": 1,
            "ready_template_count": 0,
            "failed_template_count": 1,
            "templates": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_list = root / "tasks.txt"
            task_list.write_text("# selected tasks\ntask-a\n", encoding="utf-8")
            stdout = io.StringIO()
            with (
                patch.object(batch.manager, "client_from_environment"),
                patch.object(batch, "materialize_batch", return_value=result),
            ):
                status = batch.main(
                    [
                        "--mapping",
                        str(root / "mapping.json"),
                        "--task-list",
                        str(task_list),
                    ],
                    stdout=stdout,
                    stderr=io.StringIO(),
                )

        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
