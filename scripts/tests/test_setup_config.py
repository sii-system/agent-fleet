import json
import tempfile
import unittest
from pathlib import Path

from scripts import setup_config


class SetupConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_legacy_claude_config_reads_only_managed_legacy_exports(self):
        bashrc = self.root / ".bashrc"
        legacy_prefix = "T" + "B_CC_"
        bashrc.write_text(
            "export OUTSIDE=ignored\n"
            "# >>> agent-fleet env >>>\n"
            f"export {legacy_prefix}OPIK_ENABLE_HOOK=1\n"
            f"export {legacy_prefix}CLAUDE_TGZ_SOURCE='/tmp/claude code.tgz'\n"
            "export KEEP_ME=yes\n"
            "# <<< agent-fleet env <<<\n",
            encoding="utf-8",
        )

        values = setup_config.legacy_claude_config(bashrc)

        self.assertEqual(
            values,
            [
                ("HARBOR_CC_OPIK_ENABLE_HOOK", "1"),
                ("HARBOR_CC_CLAUDE_TGZ_SOURCE", "/tmp/claude code.tgz"),
            ],
        )

    def test_merge_pi_config_preserves_custom_values(self):
        settings_path = self.root / "settings.json"
        models_path = self.root / "models.json"
        settings_path.write_text(
            json.dumps({"theme": "light", "custom": True}), encoding="utf-8"
        )
        models_path.write_text(
            json.dumps({"providers": {"other": {"models": []}}}),
            encoding="utf-8",
        )

        setup_config.merge_pi_config(
            settings_path,
            models_path,
            "https://gateway.example.invalid",
            "test-model",
        )

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "light")
        self.assertTrue(settings["custom"])
        self.assertEqual(settings["defaultProvider"], "sii-gateway")
        models = json.loads(models_path.read_text(encoding="utf-8"))
        self.assertIn("other", models["providers"])
        provider = models["providers"]["sii-gateway"]
        self.assertEqual(provider["baseUrl"], "https://gateway.example.invalid/v1")
        self.assertEqual(provider["models"][0]["id"], "test-model")

    def test_update_bashrc_replaces_managed_block_and_shell_quotes_values(self):
        bashrc = self.root / ".bashrc"
        bashrc.write_text(
            "export KEEP_ME=yes\n"
            "# >>> agent-fleet env >>>\n"
            "export OLD_VALUE=remove-me\n"
            "# <<< agent-fleet env <<<\n",
            encoding="utf-8",
        )
        environ = {
            "AUTH_TOKEN": "fake token with spaces",
            "AGENT_FLEET_PATHS_FILE": "/tmp/agent fleet/paths.env",
            "CLAUDE_TGZ_SOURCE": "/tmp/claude code.tgz",
            "CLAUDE_WHEEL_DIR_SOURCE": "/tmp/wheel dir",
        }

        setup_config.update_bashrc(bashrc, environ)

        content = bashrc.read_text(encoding="utf-8")
        self.assertIn("export KEEP_ME=yes", content)
        self.assertNotIn("OLD_VALUE", content)
        self.assertIn("export AGENT_FLEET_PATHS_FILE='/tmp/agent fleet/paths.env'", content)
        self.assertIn("export AGENT_FLEET_API_KEY='fake token with spaces'", content)
        self.assertIn("export HARBOR_CC_CLAUDE_TGZ_SOURCE='/tmp/claude code.tgz'", content)
        self.assertEqual(content.count("# >>> agent-fleet env >>>"), 1)

    def test_merge_local_config_preserves_structure_and_updates_managed_keys(self):
        path = self.root / "config.local.env"
        path.write_text(
            "# keep comment\n"
            "KEEP_SETTING=yes\n"
            "BASE_URL=https://old.invalid\n"
            "API_KEY=old-secret\n",
            encoding="utf-8",
        )
        environ = {
            "BASE_URL": "https://gateway.example.invalid/",
            "AUTH_TOKEN": "fake-new-secret",
            "MODEL": "test-model",
            "TRACE_TO_OPIK": "false",
            "OPIK_URL": "",
        }

        setup_config.merge_local_config(path, environ)

        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "# keep comment\n"
            "KEEP_SETTING=yes\n"
            "BASE_URL=https://gateway.example.invalid\n"
            "API_KEY=fake-new-secret\n"
            "MODEL=test-model\n"
            "TRACE_TO_OPIK=false\n",
        )

    def test_setup_shell_contains_no_embedded_python_programs(self):
        setup_shell = Path(__file__).resolve().parents[1] / "setup.sh"
        content = setup_shell.read_text(encoding="utf-8")

        self.assertNotIn("python3 - <<", content)
        self.assertNotIn("python3 - \"$bashrc\" <<", content)


if __name__ == "__main__":
    unittest.main()
