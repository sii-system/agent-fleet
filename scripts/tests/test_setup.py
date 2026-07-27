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
prefix=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "--prefix" ]]; then
    prefix="$argument"
    break
  fi
  previous="$argument"
done
if [[ -n "$prefix" ]]; then
  mkdir -p "$prefix/bin"
  cat >"$prefix/bin/pi" <<'PI'
#!/usr/bin/env bash
cat "$SETUP_TEST_STATE/pi-version"
PI
  chmod +x "$prefix/bin/pi"
fi
printf '0.81.1\n' >"$SETUP_TEST_STATE/pi-version"
""",
        )
        self.write_executable(
            "pi",
            """#!/usr/bin/env bash
printf 'called\n' >>"$SETUP_TEST_STATE/shadow-pi.log"
echo '9.9.9'
""",
        )
        self.write_executable(
            "git",
            """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SETUP_TEST_STATE/git.log"
""",
        )
        for command in ("curl", "jq"):
            self.write_executable(command, "#!/usr/bin/env bash\nexit 0\n")
        self.write_executable("uv", "#!/usr/bin/env bash\necho 'uv 0.11.28'\n")
        self.write_executable("uvx", "#!/usr/bin/env bash\necho 'uvx 0.11.28'\n")
        self.write_executable(
            "zellij", "#!/usr/bin/env bash\necho 'zellij 0.44.3'\n"
        )
        # The real util-linux `script` probes its stdin and can consume
        # subprocess.run(input=...) fixtures before setup reaches its prompts.
        self.write_executable("script", "#!/usr/bin/env bash\nexit 0\n")
        self.write_executable(
            "docker",
            """#!/usr/bin/env bash
if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
  echo "2.30.0"
fi
if [[ "${SETUP_TEST_DOCKER_DENY:-0}" == "1" && "${1:-}" == "ps" ]]; then
  exit 1
fi
exit 0
""",
        )

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
        paths_dir = self.home / ".config" / "agent-fleet"
        paths_dir.mkdir(parents=True)
        (paths_dir / "paths.env").write_text(
            "# Managed by agent-fleet scripts/prerequisites.sh\n"
            f"export AGENT_FLEET_BIN_DIR={self.home / '.local' / 'bin'}\n",
            encoding="utf-8",
        )
        (self.repo / "config.local.env").write_text(
            "# keep comment\n"
            "KEEP_SETTING=yes\n"
            "BASE_URL=https://old.invalid\n"
            "API_KEY=old-secret\n"
            "MODEL=old-model\n"
            "TRACE_TO_OPIK=true\n",
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

    def setup_env(self):
        env = os.environ.copy()
        for name in (
            "BASE_URL",
            "API_KEY",
            "AUTH_TOKEN",
            "MODEL",
            "TRACE_TO_OPIK",
            "OPIK_URL",
            "OPIK_API_KEY",
            "OPIK_WORKSPACE",
            "OPIK_PROJECT_NAME",
            "CLAUDE_TGZ_SOURCE",
            "CLAUDE_WHEEL_DIR_SOURCE",
            "TB_CC_CLAUDE_TGZ_SOURCE",
            "TB_CC_PY_WHEEL_DIR_SOURCE",
        ):
            env.pop(name, None)
        env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{env['PATH']}",
                "HOME": str(self.home),
                "REPO_DIR": str(self.repo),
                "HARBOR_RUNNER_SETUP": "0",
                "SETUP_TEST_STATE": str(self.state),
                "AGENT_FLEET_RUNTIME_DIR": str(self.root / "runtime"),
                "AGENT_FLEET_BIN_DIR": str(self.home / ".local" / "bin"),
                "AGENT_FLEET_PREREQUISITES_INSTALL_MANAGED": "0",
            }
        )
        return env

    def test_setup_installs_pi_and_preserves_task_container_claude_artifacts(self):
        env = self.setup_env()
        env.update(
            {
                "BASE_URL": "https://gateway.example.invalid",
                "API_KEY": "fake-setup-secret",
                "MODEL": "test-model",
                "TRACE_TO_OPIK": "false",
                "CLAUDE_TGZ_SOURCE": str(self.claude_tgz),
                "CLAUDE_WHEEL_DIR_SOURCE": str(self.wheel_dir),
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
        self.assertIn("Migrating managed executables", result.stdout)
        self.assertIn("install 24", (self.state / "nvm.log").read_text(encoding="utf-8"))
        npm_log = (self.state / "npm.log").read_text(encoding="utf-8")
        managed_npm = self.home / ".cache" / "agent-fleet" / "npm"
        self.assertIn(
            f"install -g --prefix {managed_npm} --ignore-scripts "
            "@earendil-works/pi-coding-agent@0.81.1 --force",
            npm_log,
        )
        self.assertFalse((self.state / "shadow-pi.log").exists())
        self.assertNotIn("anthropic-ai", npm_log)
        git_log = (self.state / "git.log").read_text(encoding="utf-8")
        self.assertIn("submodule sync --recursive", git_log)
        self.assertIn("submodule update --init --recursive", git_log)

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
        paths_file = self.home / ".config" / "agent-fleet" / "paths.env"
        self.assertIn(f"export AGENT_FLEET_PATHS_FILE={paths_file}", bashrc)
        paths = paths_file.read_text(encoding="utf-8")
        managed_bin = self.home / ".local" / "share" / "agent-fleet" / "bin"
        self.assertIn(f"export AGENT_FLEET_BIN_DIR={managed_bin}", paths)
        self.assertIn(f"export AGENT_FLEET_NODE_BIN_DIR={self.bin_dir}", paths)
        self.assertIn(
            f"export AGENT_FLEET_NPM_BIN_DIR={managed_npm / 'bin'}", paths
        )
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
        self.assertIn("TRACE_TO_OPIK=false", config)

        denied_env = env.copy()
        denied_env["SETUP_TEST_DOCKER_DENY"] = "1"
        denied = subprocess.run(
            [str(SETUP)],
            cwd=self.repo,
            env=denied_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("cannot access the Docker daemon", denied.stderr)
        self.assertNotIn("Environment setup complete", denied.stdout)

    def test_setup_reuses_existing_config_and_defaults_opik_tracing_off(self):
        original = (
            "# existing values\n"
            "KEEP_SETTING=yes\n"
            "BASE_URL=https://existing.example.invalid\n"
            "API_KEY=fake-existing-secret\n"
            "MODEL=existing-model\n"
        )
        config_path = self.repo / "config.local.env"
        config_path.write_text(original, encoding="utf-8")

        result = subprocess.run(
            [str(SETUP)],
            cwd=self.repo,
            env=self.setup_env(),
            input="\n",
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Opik tracing disabled", result.stdout)
        config = config_path.read_text(encoding="utf-8")
        self.assertIn("KEEP_SETTING=yes", config)
        self.assertIn("BASE_URL=https://existing.example.invalid", config)
        self.assertIn("API_KEY=fake-existing-secret", config)
        self.assertIn("MODEL=existing-model", config)
        self.assertIn("TRACE_TO_OPIK=false", config)

        models = json.loads(
            (self.home / ".pi" / "agent" / "models.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            models["providers"]["sii-gateway"]["models"][0]["id"],
            "existing-model",
        )

        backup = self.repo / "config.local.env.bak.agent-fleet"
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_setup_prompts_for_opik_url_when_tracing_is_enabled(self):
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://existing.example.invalid\n"
            "API_KEY=fake-existing-secret\n"
            "MODEL=existing-model\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [str(SETUP)],
            cwd=self.repo,
            env=self.setup_env(),
            input="yes\nhttps://opik.example.invalid/api\n",
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Opik tracing enabled", result.stdout)
        config = (self.repo / "config.local.env").read_text(encoding="utf-8")
        self.assertIn("TRACE_TO_OPIK=true", config)
        self.assertIn("OPIK_URL=https://opik.example.invalid/api", config)
        self.assertIn("OPIK_WORKSPACE=default", config)

    def test_setup_rejects_enabled_tracing_without_opik_url(self):
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://existing.example.invalid\n"
            "API_KEY=fake-existing-secret\n"
            "MODEL=existing-model\n",
            encoding="utf-8",
        )
        env = self.setup_env()
        env["TRACE_TO_OPIK"] = "true"

        result = subprocess.run(
            [str(SETUP)],
            cwd=self.repo,
            env=env,
            input="",
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Opik tracing was enabled but OPIK_URL is empty",
            result.stderr,
        )

    def test_setup_config_backup_is_ignored(self):
        gitignore = SETUP.parents[1] / ".gitignore"
        patterns = gitignore.read_text(encoding="utf-8").splitlines()
        self.assertIn("*.local.env.bak.agent-fleet", patterns)


if __name__ == "__main__":
    unittest.main()
