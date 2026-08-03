import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER = REPO_ROOT / "scripts/config_loader.sh"
HARBOR_ENV = REPO_ROOT / "Agents/utils/common/Harbor/env.sh"
CONFIG_NAMES = (
    "BASE_URL",
    "API_KEY",
    "MODEL",
    "AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "TB_MODEL",
    "TB_API_BASE",
    "TB_ANTHROPIC_BASE_URL",
    "TB_ANTHROPIC_AUTH_TOKEN",
    "HARBOR_ANALYZER_BASE_URL",
    "HARBOR_ANALYZER_MODEL",
    "ROLLOUT",
    "RL_ENV_FILE",
    "RL_API_BASE",
    "RL_API_KEY",
    "RL_MODEL_NAME",
    "AGENT_FLEET_CONFIG_LOADED_ROOT",
)


class ConfigLoaderTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_root = self.root / "repo"
        self.config_root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def clean_env(self, extra=None):
        env = os.environ.copy()
        for name in CONFIG_NAMES:
            env.pop(name, None)
        env.update(extra or {})
        return env

    def run_loader(self, script, *, extra_env=None):
        return subprocess.run(
            [
                "bash",
                "-c",
                f'source "$1"; {script}',
                "bash",
                str(LOADER),
                str(self.config_root),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=self.clean_env(extra_env),
        )

    def write_configs(self, public, local):
        (self.config_root / "config.env").write_text(public, encoding="utf-8")
        (self.config_root / "config.local.env").write_text(local, encoding="utf-8")

    def test_config_precedence(self):
        self.write_configs(
            "BASE_URL=https://public.example.invalid\n"
            "API_KEY=fake-public-key\n"
            "MODEL=public-model\n",
            "BASE_URL=https://saved.example.invalid\n"
            "API_KEY=fake-saved-key\n"
            "MODEL=saved-model\n",
        )
        command = (
            'agent_fleet_load_config "$2"; '
            'printf "%s|%s|%s" "$BASE_URL" "$API_KEY" "$MODEL"'
        )

        saved = self.run_loader(command)
        runtime = self.run_loader(
            command,
            extra_env={
                "BASE_URL": "https://runtime.example.invalid",
                "API_KEY": "fake-runtime-key",
                "MODEL": "runtime-model",
            },
        )

        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(
            saved.stdout,
            "https://saved.example.invalid|fake-saved-key|saved-model",
        )
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        self.assertEqual(
            runtime.stdout,
            "https://runtime.example.invalid|fake-runtime-key|runtime-model",
        )

    def test_tool_aliases_do_not_override_saved_canonical_config(self):
        self.write_configs(
            "",
            "BASE_URL=https://saved.example.invalid\n"
            "API_KEY=fake-saved-key\n"
            "MODEL=saved-model\n",
        )

        result = self.run_loader(
            'agent_fleet_load_config "$2"; '
            "agent_fleet_apply_auth_token_fallback; "
            'printf "%s|%s|%s" "$BASE_URL" "$API_KEY" "$MODEL"',
            extra_env={
                "ANTHROPIC_BASE_URL": "https://alias.example.invalid",
                "AUTH_TOKEN": "fake-auth-token",
                "ANTHROPIC_AUTH_TOKEN": "fake-anthropic-token",
                "TB_MODEL": "alias-model",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "https://saved.example.invalid|fake-saved-key|saved-model",
        )

    def test_direct_harbor_uses_effective_aliases(self):
        env = self.clean_env(
            {
                "HOME": str(self.root / "home"),
                "OUTPUT_ROOT": str(self.root / "runs"),
                "RUN_ID": "config-loader-test",
                "AGENT_FLEET_PATHS_FILE": str(self.root / "missing-paths.env"),
                "AGENT_FLEET_RUNTIME_DIR": str(self.root / "runtime"),
                "AGENT_FLEET_CONFIG_LOADED_ROOT": str(REPO_ROOT),
                "ANTHROPIC_BASE_URL": "https://runtime.example.invalid/v1",
                "ANTHROPIC_AUTH_TOKEN": "fake-runtime-key",
                "TB_MODEL": "runtime-model",
                "ROLLOUT": "1",
                "TRACE_TO_OPIK": "false",
            }
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    'source "$1"; printf "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s" '
                    '"$BASE_URL" "$API_KEY" "$MODEL" '
                    '"$TB_ANTHROPIC_BASE_URL" "$TB_ANTHROPIC_AUTH_TOKEN" '
                    '"$TB_MODEL" "$TB_API_BASE" '
                    '"$HARBOR_ANALYZER_BASE_URL" "$HARBOR_ANALYZER_MODEL" '
                    '"$RL_API_BASE" "$RL_API_KEY" "$RL_MODEL_NAME"'
                ),
                "bash",
                str(HARBOR_ENV),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "|xxx|minimax2.7"
            "|https://runtime.example.invalid|fake-runtime-key|runtime-model"
            "|https://runtime.example.invalid/v1/chat/completions"
            "|https://runtime.example.invalid/v1|runtime-model"
            "|https://runtime.example.invalid/v1|fake-runtime-key|runtime-model",
        )


if __name__ == "__main__":
    unittest.main()
