import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agents.utils.common.Harbor import harbor_shell_utils


class HarborShellUtilsTest(unittest.TestCase):
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

    def test_model_host_strips_credentials_port_and_path(self):
        self.assertEqual(harbor_shell_utils.agent_model_host({
            "AGENT": "claude-code",
            "HARBOR_ANTHROPIC_BASE_URL": "https://user:fake-key@gateway.example:8443/v1?key=fake",
        }), "gateway.example")
        self.assertEqual(harbor_shell_utils.agent_model_host({
            "AGENT": "dsh-sdk-minimal",
            "BASE_URL": "http://10.0.0.5:8080/v1",
            "HARBOR_ANTHROPIC_BASE_URL": "https://unused.example",
        }), "10.0.0.5")

    def test_model_host_uses_selected_provider_override(self):
        cases = [
            {"AGENT": "opencode", "HARBOR_MODEL": "custom/test",
             "OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {
                 "custom": {"options": {"baseURL": "https://selected.example/v1"}},
                 "unused": {"options": {"baseURL": "https://unused.example/v1"}},
             }})},
            {"AGENT": "pi", "PI_PROVIDER": "custom",
             "PI_MODELS_CONFIG": json.dumps({"providers": {
                 "custom": {"baseUrl": "https://selected.example/v1"},
                 "unused": {"baseUrl": "https://unused.example/v1"},
             }})},
        ]
        for values in cases:
            with self.subTest(agent=values["AGENT"]):
                values["HARBOR_ANTHROPIC_BASE_URL"] = "https://fallback.example"
                self.assertEqual(harbor_shell_utils.agent_model_host(values), "selected.example")

    def test_model_host_handles_missing_and_invalid_configuration(self):
        self.assertEqual(harbor_shell_utils.agent_model_host({"AGENT": "claude-code"}), "")
        self.assertEqual(harbor_shell_utils.agent_model_host({
            "AGENT": "oracle", "HARBOR_ANTHROPIC_BASE_URL": "https://unused.example",
        }), "")
        for url in ("/v1", "gateway.example/v1", "file:///tmp/model", "https:///v1"):
            with self.subTest(url=url):
                self.assertEqual(harbor_shell_utils.agent_model_host({
                    "AGENT": "claude-code", "HARBOR_ANTHROPIC_BASE_URL": url,
                }), "")

    def test_invalid_model_config_cli_does_not_print_credentials(self):
        error = io.StringIO()
        with patch.dict("os.environ", {
            "AGENT": "opencode",
            "OPENCODE_CONFIG_CONTENT": '{"apiKey":"fake-private-key",',
        }, clear=True), patch("sys.argv", ["helper", "agent-model-host"]), \
                contextlib.redirect_stderr(error):
            self.assertEqual(harbor_shell_utils.main(), 1)
        self.assertEqual(error.getvalue(), "Invalid model gateway configuration\n")

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
