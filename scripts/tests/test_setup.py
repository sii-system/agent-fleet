import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.bin_dir = self.root / "bin"
        self.state = self.root / "state"
        for path in (self.home, self.repo / ".git", self.bin_dir, self.state):
            path.mkdir(parents=True)

        for skill in (
            "harbor-benchmark-runner",
            "openclaw-fleet-operations",
            "openclaw-benchmark-runners",
        ):
            skill_dir = self.repo / "skills" / skill
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")

        (self.state / "node-version").write_text("v22.18.0\n", encoding="utf-8")
        (self.state / "pi-version").write_text("0.80.0\n", encoding="utf-8")
        self.write_executable(
            "node",
            """#!/usr/bin/env bash
cat "$SETUP_TEST_STATE/node-version"
""",
        )
        self.write_executable(
            "nvm",
            """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SETUP_TEST_STATE/nvm.log"
if [[ "${1:-}" == "install" ]]; then
  printf 'v24.0.0\n' >"$SETUP_TEST_STATE/node-version"
fi
""",
        )
        self.write_executable(
            "npm",
            """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SETUP_TEST_STATE/npm.log"
printf '0.81.1\n' >"$SETUP_TEST_STATE/pi-version"
""",
        )
        self.write_executable(
            "pi",
            """#!/usr/bin/env bash
cat "$SETUP_TEST_STATE/pi-version"
""",
        )
        self.write_executable(
            "git",
            """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SETUP_TEST_STATE/git.log"
""",
        )
        for command in ("curl", "jq", "docker"):
            self.write_executable(command, "#!/usr/bin/env bash\nexit 0\n")

        pi_dir = self.home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "light",
                    "customSetting": True,
                    "enableInstallTelemetry": True,
                }
            ),
            encoding="utf-8",
        )
        (pi_dir / "models.json").write_text(
            json.dumps(
                {
                    "customRoot": "preserve-me",
                    "providers": {
                        "other-provider": {
                            "baseUrl": "https://other.invalid/v1",
                            "api": "openai-completions",
                            "apiKey": "other",
                            "models": [{"id": "other-model"}],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        self.claude_sentinel = claude_dir / "settings.json"
        self.claude_sentinel.write_text('{"keep":"unchanged"}\n', encoding="utf-8")

        (self.home / ".bashrc").write_text(
            "export KEEP_ME=yes\n"
            "# >>> agent-fleet env >>>\n"
            "export ANTHROPIC_AUTH_TOKEN=old-secret\n"
            "# <<< agent-fleet env <<<\n",
            encoding="utf-8",
        )
        (self.repo / "config.local.env").write_text(
            "# keep comment\nKEEP_SETTING=yes\nBASE_URL=https://old.invalid\n",
            encoding="utf-8",
        )

        self.claude_tgz = self.root / "claude-code.tgz"
        self.claude_tgz.write_text("fixture", encoding="utf-8")
        self.wheel_dir = self.root / "wheels"
        (self.wheel_dir / "npm-cache").mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def write_executable(self, name: str, content: str) -> None:
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def test_setup_installs_pi_and_preserves_task_container_claude_artifacts(self):
        env = os.environ.copy()
        for name in (
            "AUTH_TOKEN",
            "TB_CC_CLAUDE_TGZ_SOURCE",
            "TB_CC_PY_WHEEL_DIR_SOURCE",
        ):
            env.pop(name, None)
        env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{env['PATH']}",
                "HOME": str(self.home),
                "REPO_DIR": str(self.repo),
                "BASE_URL": "https://gateway.example.invalid",
                "API_KEY": "fake-setup-secret",
                "MODEL": "test-model",
                "TRACE_TO_OPIK": "false",
                "HARBOR_RUNNER_SETUP": "0",
                "CLAUDE_TGZ_SOURCE": str(self.claude_tgz),
                "CLAUDE_WHEEL_DIR_SOURCE": str(self.wheel_dir),
                "SETUP_TEST_STATE": str(self.state),
            }
        )

        result = subprocess.run(
            [str(SETUP)],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install 24", (self.state / "nvm.log").read_text(encoding="utf-8"))
        npm_log = (self.state / "npm.log").read_text(encoding="utf-8")
        self.assertIn(
            "install -g --ignore-scripts @earendil-works/pi-coding-agent@0.81.1 --force",
            npm_log,
        )
        self.assertNotIn("anthropic-ai", npm_log)

        pi_dir = self.home / ".pi" / "agent"
        settings = json.loads((pi_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["defaultProvider"], "sii-gateway")
        self.assertEqual(settings["defaultModel"], "test-model")
        self.assertEqual(settings["theme"], "light")
        self.assertTrue(settings["customSetting"])
        self.assertTrue(settings["enableInstallTelemetry"])

        models = json.loads((pi_dir / "models.json").read_text(encoding="utf-8"))
        self.assertEqual(models["customRoot"], "preserve-me")
        self.assertIn("other-provider", models["providers"])
        provider = models["providers"]["sii-gateway"]
        self.assertEqual(provider["baseUrl"], "https://gateway.example.invalid/v1")
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$AGENT_FLEET_API_KEY")
        self.assertEqual(provider["models"][0]["id"], "test-model")
        self.assertNotIn("fake-setup-secret", (pi_dir / "models.json").read_text())

        bashrc = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertIn("export KEEP_ME=yes", bashrc)
        self.assertIn("export PI_OFFLINE=1", bashrc)
        self.assertIn("export AGENT_FLEET_API_KEY=fake-setup-secret", bashrc)
        self.assertIn(f"export TB_CC_CLAUDE_TGZ_SOURCE={self.claude_tgz}", bashrc)
        self.assertIn(f"export TB_CC_PY_WHEEL_DIR_SOURCE={self.wheel_dir}", bashrc)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", bashrc)

        for skill in (
            "harbor-benchmark-runner",
            "openclaw-fleet-operations",
            "openclaw-benchmark-runners",
        ):
            skill_link = pi_dir / "skills" / skill
            self.assertTrue(skill_link.is_symlink())
            self.assertEqual(skill_link.resolve(), (self.repo / "skills" / skill).resolve())

        self.assertEqual(
            self.claude_sentinel.read_text(encoding="utf-8"),
            '{"keep":"unchanged"}\n',
        )
        config = (self.repo / "config.local.env").read_text(encoding="utf-8")
        self.assertIn("KEEP_SETTING=yes", config)
        self.assertIn("BASE_URL=https://gateway.example.invalid", config)
        self.assertIn("API_KEY=fake-setup-secret", config)
        self.assertIn("MODEL=test-model", config)


if __name__ == "__main__":
    unittest.main()
