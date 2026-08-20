from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_template_mapping as mapping


class QzTemplateMappingTest(unittest.TestCase):
    def make_task(self, root: Path, name: str, image: str | None) -> Path:
        task = root / name
        task.mkdir(parents=True)
        environment = "[environment]\n"
        if image is not None:
            environment += f"docker_image = {json.dumps(image)}\n"
        (task / "task.toml").write_text(environment, encoding="utf-8")
        return task

    def test_inventory_deduplicates_identical_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_task(root, "task-a", "ubuntu:24.04")
            second = self.make_task(root, "task-b", "ubuntu:24.04")

            inventory = mapping.build_inventory(
                benchmark="terminalbench21",
                tasks=[("task-b", second), ("task-a", first)],
            )

        self.assertEqual(list(inventory["tasks"]), ["task-a", "task-b"])
        self.assertEqual(len(inventory["templates"]), 1)
        template_keys = {task["template_key"] for task in inventory["tasks"].values()}
        self.assertEqual(len(template_keys), 1)

    def test_identity_changes_with_every_template_input(self):
        baseline = mapping.template_identity("ubuntu:24.04", "g.c1", "official")
        self.assertEqual(
            baseline,
            "916dae526a531141ceeeacb0b68f7a2830eca2a2d38754084b8db3ea7d2e488f",
        )
        self.assertEqual(
            mapping.template_name("ubuntu:24.04", baseline),
            "af_ubuntu_24_04_916dae526a531141",
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:22.04", "g.c1", "official"),
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:24.04", "g.c2", "official"),
        )
        self.assertNotEqual(
            baseline,
            mapping.template_identity("ubuntu:24.04", "g.c1", "custom"),
        )

    def test_template_name_is_stable_and_qz_safe(self):
        identity = mapping.template_identity(
            "registry.example/team/task-image:v1", "g.c1", "official"
        )
        first = mapping.template_name("registry.example/team/task-image:v1", identity)
        second = mapping.template_name("registry.example/team/task-image:v1", identity)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), mapping.TEMPLATE_NAME_MAX_LENGTH)
        self.assertRegex(first, re.compile(r"^[A-Za-z0-9_]+$"))

    def test_discover_tasks_can_follow_a_task_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_task(root, "task-a", "ubuntu:24.04")
            self.make_task(root, "task-b", "debian:12")
            task_list = root / "selected.txt"
            task_list.write_text("# smoke\ntask-b\n", encoding="utf-8")

            tasks = mapping.discover_tasks(root, task_list)

        self.assertEqual([key for key, _ in tasks], ["task-b"])

    def test_missing_image_reports_every_unsupported_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_task(root, "task-a", None)
            second = self.make_task(root, "task-b", None)

            with self.assertRaises(mapping.QzTemplateMappingError) as context:
                mapping.build_inventory(
                    benchmark="terminalbench21",
                    tasks=[("task-a", first), ("task-b", second)],
                )

        message = str(context.exception)
        self.assertIn("cannot inventory 2 task(s)", message)
        self.assertIn("task-a", message)
        self.assertIn("task-b", message)

    def test_single_task_dataset_uses_directory_name_as_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "task.toml").write_text(
                '[environment]\ndocker_image = "ubuntu:24.04"\n',
                encoding="utf-8",
            )

            tasks = mapping.discover_tasks(root)

        self.assertEqual(tasks, [(root.name, root.resolve())])

    def test_regeneration_preserves_only_matching_template_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            arguments = [
                "--dataset-root",
                str(root),
                "--benchmark",
                "terminalbench21",
                "--output",
                str(output),
            ]
            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            template_key = payload["tasks"]["task-a"]["template_key"]
            payload["templates"][template_key]["template_id"] = "template-1"
            output.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            regenerated = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            regenerated["templates"][template_key]["template_id"],
            "template-1",
        )

    def test_regeneration_refuses_invalid_existing_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            arguments = [
                "--dataset-root",
                str(root),
                "--benchmark",
                "terminalbench21",
                "--output",
                str(output),
            ]
            self.assertEqual(mapping.main(arguments, stderr=io.StringIO()), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            template = next(iter(payload["templates"].values()))
            template["template_id"] = " "
            invalid = json.dumps(payload)
            output.write_text(invalid, encoding="utf-8")

            stderr = io.StringIO()
            result = mapping.main(arguments, stderr=stderr)

            self.assertEqual(output.read_text(encoding="utf-8"), invalid)

        self.assertEqual(result, 1)
        self.assertIn("invalid template_id", stderr.getvalue())

    def test_cli_writes_deterministic_mapping_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", "ubuntu:24.04")
            output = Path(temporary) / "mapping.json"
            stderr = io.StringIO()

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "terminalbench21",
                    "--output",
                    str(output),
                ],
                stdout=io.StringIO(),
                stderr=stderr,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["identity_version"], "qz-template-image-v1")
        template = next(iter(payload["templates"].values()))
        self.assertIsNone(template["template_id"])
        self.assertIn("1 tasks and 1 unique images", stderr.getvalue())

    def test_cli_does_not_write_partial_output_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            root.mkdir()
            self.make_task(root, "task-a", None)
            output = Path(temporary) / "mapping.json"
            stderr = io.StringIO()

            result = mapping.main(
                [
                    "--dataset-root",
                    str(root),
                    "--benchmark",
                    "terminalbench21",
                    "--output",
                    str(output),
                ],
                stdout=io.StringIO(),
                stderr=stderr,
            )
            output_exists = output.exists()

        self.assertEqual(result, 1)
        self.assertFalse(output_exists)
        self.assertIn("environment.docker_image", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
