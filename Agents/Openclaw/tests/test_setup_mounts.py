import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

OPENCLAW_DIR = Path(__file__).resolve().parents[1]


class OpenClawSetupMountTests(unittest.TestCase):
    def test_state_and_workspace_mounts_do_not_overlay_openclaw_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "openclaw"
            shutil.copytree(OPENCLAW_DIR / "scripts", project_dir / "scripts")
            shutil.copytree(OPENCLAW_DIR / "config", project_dir / "config")

            config_base = tmp_path / "config"
            workspace_base = tmp_path / "workspace"
            env = os.environ.copy()
            env.update(
                {
                    "BASE_URL": "https://example.invalid/v1",
                    "API_KEY": "test-key",
                    "MODEL": "test-model",
                    "CONFIG_BASE": str(config_base),
                    "WORKSPACE_BASE": str(workspace_base),
                }
            )

            subprocess.run(
                [str(project_dir / "scripts" / "setup.sh"), "1"],
                cwd=OPENCLAW_DIR.parent.parent,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            compose = (project_dir / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertNotIn(f"- {config_base / '1'}:/home/node/.openclaw", compose)
            self.assertIn(f"- {config_base / '1'}:/home/node/openclaw-state", compose)
            self.assertIn(f"- {workspace_base / '1'}:/home/node/workspace", compose)
            self.assertIn("OPENCLAW_STATE_DIR: /home/node/openclaw-state", compose)
            self.assertIn(
                "OPENCLAW_CONFIG_PATH: /home/node/openclaw-state/openclaw.json",
                compose,
            )
            # .openclaw must be mounted from openclaw-home (not config_dir) so
            # the exec tool's chmod works on a writable volume.
            openclaw_home = config_base / "1" / "openclaw-home"
            self.assertIn(f"- {openclaw_home}:/home/node/.openclaw", compose)
            self.assertTrue(openclaw_home.is_dir())
            # The agents symlink must be recreated inside the mounted dir so
            # session tooling can still discover sessions.
            agents_link = openclaw_home / "agents"
            self.assertTrue(agents_link.is_symlink())
            self.assertEqual(os.readlink(str(agents_link)), "../openclaw-state/agents")
            # Container must run as host user so chmod succeeds.
            self.assertIn(f'user: "{os.getuid()}:{os.getgid()}"', compose)

            config = (config_base / "1" / "openclaw.json").read_text(encoding="utf-8")
            parsed_config = json.loads(config)
            provider = parsed_config["models"]["providers"]["default"]
            self.assertEqual(provider["models"][0]["id"], "test-model")
            self.assertIn('"workspace": "/home/node/workspace"', config)
            self.assertEqual(parsed_config["agents"]["defaults"]["heartbeat"]["every"], "0m")
            self.assertIn('"allow": [', config)
            self.assertIn('"openai"', config)
            if os.name == "posix" and os.uname().sysname == "Linux":
                self.assertTrue((config_base / "1").stat().st_mode & stat.S_IWOTH)
                self.assertTrue((workspace_base / "1").stat().st_mode & stat.S_IWOTH)

    def test_setup_allows_heartbeat_cadence_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "openclaw"
            shutil.copytree(OPENCLAW_DIR / "scripts", project_dir / "scripts")
            shutil.copytree(OPENCLAW_DIR / "config", project_dir / "config")

            env = os.environ.copy()
            env.update(
                {
                    "BASE_URL": "https://example.invalid/v1",
                    "API_KEY": "test-key",
                    "MODEL": "test-model",
                    "CONFIG_BASE": str(tmp_path / "config"),
                    "WORKSPACE_BASE": str(tmp_path / "workspace"),
                    "HEARTBEAT_EVERY": "30m",
                }
            )

            subprocess.run(
                [str(project_dir / "scripts" / "setup.sh"), "1"],
                cwd=OPENCLAW_DIR.parent.parent,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            config = json.loads(
                (tmp_path / "config" / "1" / "openclaw.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["agents"]["defaults"]["heartbeat"]["every"], "30m")

    def test_opik_mode_keeps_plugin_allowlist_restrictive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "openclaw"
            shutil.copytree(OPENCLAW_DIR / "scripts", project_dir / "scripts")
            shutil.copytree(OPENCLAW_DIR / "config", project_dir / "config")

            env = os.environ.copy()
            env.update(
                {
                    "BASE_URL": "https://example.invalid/v1",
                    "API_KEY": "test-key",
                    "MODEL": "test-model",
                    "CONFIG_BASE": str(tmp_path / "config"),
                    "WORKSPACE_BASE": str(tmp_path / "workspace"),
                    "OPIK_URL": "https://opik.example.invalid/api/",
                    "OPIK_PROJECT_NAME": "test-project",
                }
            )

            subprocess.run(
                [str(project_dir / "scripts" / "setup.sh"), "1"],
                cwd=OPENCLAW_DIR.parent.parent,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            config = (tmp_path / "config" / "1" / "openclaw.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"allow": [', config)
            self.assertIn('"openai"', config)
            self.assertIn('"openclaw-opik-tracer"', config)
            self.assertIn('"load": {', config)
            self.assertIn('"/opt/openclaw-plugins/openclaw-opik-tracer"', config)
            self.assertIn('"enabled": true', config)
            self.assertIn('"hooks": {', config)
            self.assertIn('"allowConversationAccess": true', config)

            compose = (project_dir / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertNotIn("opik-entrypoint", compose)
            self.assertNotIn("entrypoint:", compose)
            # openclaw-home mounts to /home/node/.openclaw (writable)
            openclaw_home = tmp_path / "config" / "1" / "openclaw-home"
            self.assertIn(f"{openclaw_home}:/home/node/.openclaw", compose)
            self.assertIn('OC_OPIK_PROCESS_TIMEOUT_S: "60"', compose)
            self.assertIn(
                f"{tmp_path / 'config' / '1' / 'opik-state'}:/home/node/.openclaw/state",
                compose,
            )

    def test_setup_handles_json_special_characters_in_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "openclaw"
            shutil.copytree(OPENCLAW_DIR / "scripts", project_dir / "scripts")
            shutil.copytree(OPENCLAW_DIR / "config", project_dir / "config")

            env = os.environ.copy()
            env.update(
                {
                    "BASE_URL": 'https://example.invalid/v1?name="quoted"&path=a\\b',
                    "API_KEY": 'test"key\\with|pipes',
                    "MODEL": 'model"with\\slashes',
                    "SANDBOX_MODE": 'mode"quoted',
                    "EXEC_SECURITY": 'deny"quoted',
                    "EXEC_ASK": 'always"quoted',
                    "CONFIG_BASE": str(tmp_path / "config"),
                    "WORKSPACE_BASE": str(tmp_path / "workspace"),
                }
            )

            subprocess.run(
                [str(project_dir / "scripts" / "setup.sh"), "1"],
                cwd=OPENCLAW_DIR.parent.parent,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            config = json.loads((tmp_path / "config" / "1" / "openclaw.json").read_text())
            provider = config["models"]["providers"]["default"]
            self.assertEqual(provider["baseUrl"], env["BASE_URL"])
            self.assertEqual(provider["apiKey"], env["API_KEY"])
            self.assertEqual(provider["models"][0]["id"], env["MODEL"])
            self.assertEqual(config["agents"]["defaults"]["sandbox"]["mode"], env["SANDBOX_MODE"])
            self.assertEqual(config["tools"]["exec"]["security"], env["EXEC_SECURITY"])
            self.assertEqual(config["tools"]["exec"]["ask"], env["EXEC_ASK"])


if __name__ == "__main__":
    unittest.main()
