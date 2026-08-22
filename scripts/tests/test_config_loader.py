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
    "HARBOR_MODEL",
    "HARBOR_API_BASE",
    "HARBOR_ANTHROPIC_BASE_URL",
    "HARBOR_ANTHROPIC_AUTH_TOKEN",
    "HARBOR_ANALYZER_BASE_URL",
    "HARBOR_ANALYZER_MODEL",
    "ROLLOUT",
    "RL_ENV_FILE",
    "RL_API_BASE",
    "RL_API_KEY",
    "RL_MODEL_NAME",
    "AGENT_FLEET_CONFIG_LOADED_ROOT",
    "TRACE_TO_OPIK",
    "OPIK_PLUGIN",
    "OPIK_MODE",
    "AGENT_FLEET_CONFIG_QUIET",
    "AGENT_FLEET_OPIK_DEPRECATION_WARNED",
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
                "HARBOR_MODEL": "alias-model",
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
                "HARBOR_MODEL": "runtime-model",
                "ROLLOUT": "1",
            }
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    'source "$1"; printf "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s" '
                    '"$BASE_URL" "$API_KEY" "$MODEL" '
                    '"$HARBOR_ANTHROPIC_BASE_URL" "$HARBOR_ANTHROPIC_AUTH_TOKEN" '
                    '"$HARBOR_MODEL" "$HARBOR_API_BASE" '
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

    def test_retired_opik_vars_warn_once_each_and_can_be_silenced(self):
        command = 'agent_fleet_load_config "$2"'

        clean = self.run_loader(command)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(clean.stderr, "")

        warned = self.run_loader(
            command,
            extra_env={
                "TRACE_TO_OPIK": "true",
                "OPIK_PLUGIN": "1",
                "OPIK_MODE": "hook",
            },
        )
        self.assertEqual(warned.returncode, 0, warned.stderr)
        for name in ("TRACE_TO_OPIK", "OPIK_PLUGIN", "OPIK_MODE"):
            self.assertIn(
                f"[WARN] {name} is no longer used; Opik tracing follows OPIK_URL",
                warned.stderr,
            )
        # The name-specific line repeats per retired var, but the trailing
        # guidance line is printed once per invocation, not once per var.
        self.assertEqual(
            warned.stderr.count(
                "[WARN] set OPIK_URL to upload traces, or leave it empty to disable"
            ),
            1,
        )

        quiet = self.run_loader(
            command,
            extra_env={
                "TRACE_TO_OPIK": "true",
                "OPIK_PLUGIN": "1",
                "OPIK_MODE": "hook",
                "AGENT_FLEET_CONFIG_QUIET": "1",
            },
        )
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stderr, "")

    def test_retired_opik_vars_warn_once_per_process_tree(self):
        # A launcher and the runner it execs both source the loader; the
        # exported AGENT_FLEET_OPIK_DEPRECATION_WARNED marker makes the
        # second call in the same process silent instead of repeating the
        # warning.
        result = self.run_loader(
            "agent_fleet_warn_retired_opik_vars; "
            "agent_fleet_warn_retired_opik_vars",
            extra_env={"TRACE_TO_OPIK": "true"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stderr.count("[WARN] TRACE_TO_OPIK is no longer used"),
            1,
        )
        self.assertEqual(
            result.stderr.count(
                "[WARN] set OPIK_URL to upload traces, or leave it empty to disable"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
