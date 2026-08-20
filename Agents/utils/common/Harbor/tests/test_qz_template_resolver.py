from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_template_mapping as mapping
import qz_template_resolver as resolver

TEST_IMAGE = "ubuntu:24.04"
TEST_SPEC = mapping.DEFAULT_SPEC
TEST_IMAGE_SOURCE = mapping.DEFAULT_IMAGE_SOURCE
TEST_IDENTITY = mapping.template_identity(TEST_IMAGE, TEST_SPEC, TEST_IMAGE_SOURCE)
TEST_TEMPLATE_NAME = mapping.template_name(TEST_IMAGE, TEST_IDENTITY)


class FakeClient:
    def __init__(self):
        self.by_id = {}
        self.by_name = {}
        self.get_template_calls = []
        self.get_by_name_calls = []

    def get_template(self, template_id):
        self.get_template_calls.append(template_id)
        return self.by_id[template_id]

    def get_by_name(self, name):
        self.get_by_name_calls.append(name)
        return self.by_name.get(name)


class QzTemplateResolverTest(unittest.TestCase):
    def make_mapping(self, root: Path) -> tuple[Path, str, dict]:
        task_dir = root / "task-a"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(
            '[environment]\ndocker_image = "ubuntu:24.04"\n',
            encoding="utf-8",
        )
        payload = mapping.build_inventory(
            benchmark="terminalbench21",
            tasks=[("task-a", task_dir)],
        )
        path = root / "mapping.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        template_key = payload["tasks"]["task-a"]["template_key"]
        return path, template_key, payload["templates"][template_key]

    @staticmethod
    def ready(
        template_id: str,
        *,
        template_name: str = TEST_TEMPLATE_NAME,
        spec: str = TEST_SPEC,
    ) -> dict:
        return {
            "templateID": template_id,
            "names": [template_name],
            "builds": [
                {
                    "createdAt": "2026-01-01T00:00:00Z",
                    "sbxSpecCode": spec,
                    "status": "ready",
                }
            ],
        }

    def test_resolve_cached_id_checks_live_ready_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_id"] = "template-1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-1")

            template_id = resolver.resolve_task_template(
                path,
                "terminal-bench/task-a",
                client,
            )

        self.assertEqual(template_id, "template-1")
        self.assertEqual(client.get_template_calls, ["template-1"])

    def test_environment_resolution_accepts_legacy_qz_key(self):
        with (
            patch.dict(
                os.environ,
                {"E2B_API_KEY": "sbx_legacy", "SBX_API_URL": "https://qz.example"},
                clear=True,
            ),
            patch.object(
                resolver,
                "resolve_task_template",
                return_value="template-1",
            ) as resolve,
        ):
            template_id = resolver.resolve_task_template_from_environment(
                Path("mapping.json"),
                "task-a",
            )

        self.assertEqual(template_id, "template-1")
        self.assertEqual(resolve.call_args.args[2].api_key, "sbx_legacy")

    def test_resolve_cached_id_rejects_mismatched_api_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_id"] = "template-1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-2")

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "returned ID",
            ):
                resolver.resolve_task_template(path, "task-a", client)

    def test_resolve_cached_id_rejects_mismatched_spec(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_id"] = "template-1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-1", spec="g.c2")

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "has spec 'g.c2', expected 'g.c1'",
            ):
                resolver.resolve_task_template(path, "task-a", client)

    def test_resolve_cached_id_rejects_missing_spec_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_id"] = "template-1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeClient()
            live = self.ready("template-1")
            del live["builds"][0]["sbxSpecCode"]
            client.by_id["template-1"] = live

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "latest build is missing sbxSpecCode",
            ):
                resolver.resolve_task_template(path, "task-a", client)

    def test_resolve_unbound_entry_uses_deterministic_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, entry = self.make_mapping(Path(temporary))
            client = FakeClient()
            client.by_name[entry["template_name"]] = self.ready("template-1")

            template_id = resolver.resolve_task_template(path, "task-a", client)

        self.assertEqual(template_id, "template-1")
        self.assertEqual(client.get_by_name_calls, [entry["template_name"]])

    def test_task_key_resolves_unique_nested_names_and_rejects_ambiguity(self):
        self.assertEqual(
            resolver.resolve_task_key(
                {"tasks": {"suite/task-a": {}}},
                "task-a",
            ),
            "suite/task-a",
        )
        self.assertEqual(
            resolver.resolve_task_key(
                {"tasks": {"task-a": {}, "suite/task-a": {}}},
                "benchmark/suite/task-a",
            ),
            "suite/task-a",
        )
        with self.assertRaisesRegex(
            resolver.QzTemplateResolutionError,
            "ambiguously matches",
        ):
            resolver.resolve_task_key(
                {"tasks": {"first/task-a": {}, "second/task-a": {}}},
                "task-a",
            )

    def test_resolve_rejects_missing_and_non_ready_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, entry = self.make_mapping(Path(temporary))
            client = FakeClient()
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "materialize or bind",
            ):
                resolver.resolve_task_template(path, "task-a", client)

            client.by_name[entry["template_name"]] = {
                "templateID": "template-1",
                "builds": [{"status": "building"}],
            }
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "is not ready",
            ):
                resolver.resolve_task_template(path, "task-a", client)

    def test_bind_validates_ready_and_persists_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-1")

            result = resolver.bind_task_template(
                path,
                "task-a",
                "template-1",
                client,
            )
            payload = resolver.load_mapping(path)

        self.assertEqual(result, "template-1")
        self.assertEqual(
            payload["templates"][template_key]["template_id"],
            "template-1",
        )

    def test_bind_rejects_alias_for_different_image(self):
        other_image = "debian:12"
        other_identity = mapping.template_identity(
            other_image,
            TEST_SPEC,
            TEST_IMAGE_SOURCE,
        )
        other_name = mapping.template_name(other_image, other_identity)
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            client = FakeClient()
            client.by_id["template-1"] = self.ready(
                "template-1",
                template_name=other_name,
            )

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "does not have expected content-derived alias",
            ):
                resolver.bind_task_template(path, "task-a", "template-1", client)
            payload = resolver.load_mapping(path)

        self.assertIsNone(payload["templates"][template_key]["template_id"])

    def test_bind_rejects_blank_template_id_without_api_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, _ = self.make_mapping(Path(temporary))
            client = FakeClient()

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "must not be empty",
            ):
                resolver.bind_task_template(path, "task-a", " ", client)

        self.assertEqual(client.get_template_calls, [])

    def test_materialize_creates_one_task_and_persists_ready_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, entry = self.make_mapping(Path(temporary))
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-1")
            stderr = io.StringIO()
            with patch.object(
                resolver.manager,
                "create_template_from_image",
                return_value="template-1",
            ) as create:
                result = resolver.materialize_task_template(
                    path,
                    "task-a",
                    client,
                    timeout=30,
                    stderr=stderr,
                )
            payload = resolver.load_mapping(path)

        self.assertEqual(result, "template-1")
        self.assertEqual(
            payload["templates"][template_key]["template_id"],
            "template-1",
        )
        create.assert_called_once_with(
            client,
            name=entry["template_name"],
            image=entry["image"],
            spec=entry["spec"],
            image_source=entry["image_source"],
            timeout=30,
            exists_ok=True,
            stderr=stderr,
        )

    def test_materialize_reuses_cached_id_without_create(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_id"] = "template-1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-1")
            with patch.object(
                resolver.manager,
                "create_template_from_image",
            ) as create:
                result = resolver.materialize_task_template(
                    path,
                    "task-a",
                    client,
                    timeout=30,
                )

        self.assertEqual(result, "template-1")
        create.assert_not_called()

    def test_materialize_rejects_mismatched_api_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, _ = self.make_mapping(Path(temporary))
            client = FakeClient()
            client.by_id["template-1"] = self.ready("template-2")
            with (
                patch.object(
                    resolver.manager,
                    "create_template_from_image",
                    return_value="template-1",
                ),
                self.assertRaisesRegex(
                    resolver.QzTemplateResolutionError,
                    "materialized ID",
                ),
            ):
                resolver.materialize_task_template(
                    path,
                    "task-a",
                    client,
                    timeout=30,
                )

    def test_bad_schema_and_unknown_task_fail_clearly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "mapping.json"
            path.write_text('{"schema_version": 2}', encoding="utf-8")
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "schema_version",
            ):
                resolver.load_mapping(path)

            path, _, _ = self.make_mapping(root)
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "not present",
            ):
                resolver.resolve_task_template(path, "missing", FakeClient())

    def test_tampered_task_image_and_template_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["tasks"]["task-a"]["docker_image"] = "debian:12"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "image does not match",
            ):
                resolver.resolve_task_template(path, "task-a", FakeClient())

            payload["tasks"]["task-a"]["docker_image"] = "ubuntu:24.04"
            payload["templates"][template_key]["spec"] = "g.c2"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "content identity",
            ):
                resolver.resolve_task_template(path, "task-a", FakeClient())

    def test_tampered_template_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, template_key, _ = self.make_mapping(Path(temporary))
            payload = resolver.load_mapping(path)
            payload["templates"][template_key]["template_name"] = "other_alias"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                resolver.QzTemplateResolutionError,
                "content-derived template_name",
            ):
                resolver.resolve_task_template(path, "task-a", FakeClient())


if __name__ == "__main__":
    unittest.main()
