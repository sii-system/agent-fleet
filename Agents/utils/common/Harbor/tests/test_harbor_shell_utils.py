import json
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import harbor_shell_utils


class HarborShellUtilsTest(unittest.TestCase):
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
