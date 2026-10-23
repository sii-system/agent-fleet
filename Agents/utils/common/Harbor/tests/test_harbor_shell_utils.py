import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import harbor_shell_utils


class HarborShellUtilsTest(unittest.TestCase):
    def test_http_preflight_preserves_route_errors_auth_responses_and_warnings(self):
        values = {"OPIK_URL_OVERRIDE": "https://opik.example/api"}
        for service, status, expected in (
            ("opik-ingestion", "401", 0), ("opik-ingestion", "422", 0),
            ("opik-ingestion", "404", 1), ("opik-ingestion", "502", 0),
            ("opik-projects", "403", 0), ("opik-projects", "502", 1),
        ):
            with self.subTest(service=service, status=status), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                result = harbor_shell_utils.check_http_preflight(service, status, environ=values)
                self.assertEqual(result, expected)
                if expected:
                    self.assertIn("[ERROR]", errors.getvalue())
                elif status == "502":
                    self.assertIn("[WARN]", errors.getvalue())

    def test_docker_http_failure_preserves_strictness_and_online_event(self):
        values = {"HARBOR_ONLINE_ANALYSIS": "1", "HARBOR_TASK_INDEX": "7"}
        for strict in (True, False):
            with self.subTest(strict=strict), \
                    contextlib.redirect_stdout(io.StringIO()) as events, \
                    contextlib.redirect_stderr(io.StringIO()):
                result = harbor_shell_utils.check_http_preflight(
                    "dockerhub", "401", auth_status="000", timeout="5",
                    strict=strict, environ=values,
                )
                self.assertEqual(result, int(strict))
                payload = json.loads(events.getvalue().removeprefix("[ONLINE_ENV] "))
                self.assertEqual(payload["fatal"], strict)
                self.assertEqual(payload["task_id"], 7)

    def test_native_task_config_preserves_exact_selection_and_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("task[1]", "task two"):
                (root / name).mkdir()
                (root / name / "task.toml").touch()
            selected = root / "tasks.txt"
            selected.write_text("task[1]\r\ntask two\n")
            config = harbor_shell_utils.native_task_config(root, selected)
            self.assertEqual(config["tasks"], [
                {"path": str(root / "task[1]")}, {"path": str(root / "task two")},
            ])
            self.assertEqual(harbor_shell_utils.native_task_config(root, selected, "1"),
                             {"tasks": config["tasks"][:1]})
            for invalid in ("", "missing\n", "../outside\n"):
                selected.write_text(invalid)
                with self.assertRaises(ValueError):
                    harbor_shell_utils.native_task_config(root, selected)

    def test_online_event_uses_task_environment(self):
        rendered = harbor_shell_utils.online_event(
            "preflight",
            "docker_registry",
            "connectivity_degraded",
            "warning",
            False,
            "continuing",
            {"HARBOR_TASK_INDEX": "7", "HARBOR_TASK_ID": "task-a"},
        )

        payload = json.loads(rendered.removeprefix("[ONLINE_ENV] "))
        self.assertEqual(payload["task_id"], 7)
        self.assertEqual(payload["task_name"], "task-a")
        self.assertFalse(payload["fatal"])

    def test_json_and_url_helpers(self):
        self.assertEqual(harbor_shell_utils.normalize_json('{"b": 2, "a": 1}'), '{"b":2,"a":1}')
        self.assertEqual(
            harbor_shell_utils.json_string_field('{"api_key":"secret"}', "api_key"),
            "secret",
        )
        self.assertEqual(
            harbor_shell_utils.url_hostname("https://opik.example.invalid/api"),
            "opik.example.invalid",
        )

    def test_readonly_mounts_include_only_ready_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            (cache / "uv").touch()
            (cache / "uvx").touch()
            package = root / "package.tgz"
            package.touch()

            mounts = harbor_shell_utils.readonly_mounts(
                [(package, "/opt/package.tgz", "exists"), (cache, "/opt/bin", "uv-bin")]
            )

            self.assertEqual(
                mounts,
                [
                    {"type": "bind", "source": str(package), "target": "/opt/package.tgz", "read_only": True},
                    {"type": "bind", "source": str(cache), "target": "/opt/bin", "read_only": True},
                ],
            )


if __name__ == "__main__":
    unittest.main()
