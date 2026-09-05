from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).parents[1]


class HarborGenerationConfigTests(unittest.TestCase):
    def _run_validation(
        self,
        agent: str,
        *,
        validation_function: str = "harbor_validate_generation_controls",
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "PATH": os.environ["PATH"],
                "HOME": temp_dir,
                "AGENT": agent,
                "MODEL": "test-model",
                "BASE_URL": "https://llm.example",
                "API_KEY": "fake-key",
                "AGENT_FLEET_PATHS_FILE": f"{temp_dir}/missing-paths.env",
                "AGENT_FLEET_RUNTIME_DIR": f"{temp_dir}/runtime",
                **overrides,
            }
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    f'source "$1"; {validation_function}',
                    "bash",
                    str(HARBOR_DIR / "env.sh"),
                ],
                check=False,
                capture_output=True,
                env=env,
                text=True,
            )

    def _load_config(
        self,
        agent: str,
        *,
        load_count: int = 1,
        **overrides: str,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "PATH": os.environ["PATH"],
                "HOME": temp_dir,
                "AGENT": agent,
                "MODEL": "test-model",
                "BASE_URL": "https://llm.example",
                "API_KEY": "fake-key",
                "AGENT_FLEET_PATHS_FILE": f"{temp_dir}/missing-paths.env",
                "AGENT_FLEET_RUNTIME_DIR": f"{temp_dir}/runtime",
                **overrides,
            }
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    """
for ((load_index = 0; load_index < $2; load_index++)); do
  source "$1"
done
python3 - <<'PY'
import json
import os

print(json.dumps({
    "llm_kwargs": json.loads(os.environ["HARBOR_LLM_KWARGS"]),
    "max_new_tokens": os.environ["HARBOR_MAX_NEW_TOKENS"],
    "model_info": json.loads(os.environ["HARBOR_MODEL_INFO"]),
    "claude_max_output_tokens": os.environ["HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
    "opencode_config": (
        json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        if os.environ["OPENCODE_CONFIG_CONTENT"]
        else None
    ),
    "opencode_runtime_secrets": json.loads(
        os.environ["OPENCODE_RUNTIME_SECRETS_JSON"]
    ),
    "harbor_model": os.environ["HARBOR_MODEL"],
    "agent_import_path": os.environ["HARBOR_AGENT_IMPORT_PATH"],
    "pi_models_config": (
        json.loads(os.environ["PI_MODELS_CONFIG"])
        if os.environ["PI_MODELS_CONFIG"]
        else None
    ),
    "pi_settings_config": (
        json.loads(os.environ["PI_SETTINGS_CONFIG"])
        if os.environ["PI_SETTINGS_CONFIG"]
        else None
    ),
    "fixer": {
        name: os.environ[name]
        for name in (
            "HARBOR_FIXER_MODEL",
            "HARBOR_FIXER_AGENT_TIMEOUT",
            "HARBOR_FIXER_EXECUTION_TIMEOUT",
            "HARBOR_FIXER_SUMMARY_LIMIT",
        )
    },
}))
PY
""",
                    "bash",
                    str(HARBOR_DIR / "env.sh"),
                    str(load_count),
                ],
                check=True,
                capture_output=True,
                env=env,
                text=True,
            )
        return json.loads(result.stdout)

    def test_fixer_defaults_and_override(self) -> None:
        config = self._load_config(
            "claude-code", HARBOR_FIXER_EXECUTION_TIMEOUT="45"
        )
        self.assertEqual(
            config["fixer"],
            {
                "HARBOR_FIXER_MODEL": "test-model",
                "HARBOR_FIXER_AGENT_TIMEOUT": "900",
                "HARBOR_FIXER_EXECUTION_TIMEOUT": "45",
                "HARBOR_FIXER_SUMMARY_LIMIT": "4000",
            },
        )

    def test_opencode_applies_sampling_and_output_token_settings(self) -> None:
        config = self._load_config(
            "opencode",
            HARBOR_TEMPERATURE="0.2",
            HARBOR_TOP_P="0.9",
            HARBOR_MAX_TOKENS="8192",
        )

        self.assertEqual(config["llm_kwargs"]["temperature"], 0.2)
        self.assertEqual(config["llm_kwargs"]["top_p"], 0.9)
        self.assertEqual(config["max_new_tokens"], "8192")
        self.assertEqual(config["model_info"]["max_output_tokens"], 8192)
        self.assertEqual(config["claude_max_output_tokens"], "8192")
        self.assertEqual(
            config["opencode_config"]["agent"]["build"],
            {"temperature": 0.2, "top_p": 0.9},
        )
        self.assertEqual(
            config["opencode_config"]["provider"]["custom"]["models"]["test-model"][
                "limit"
            ]["output"],
            8192,
        )
        self.assertRegex(
            config["opencode_config"]["provider"]["custom"]["options"]["apiKey"],
            r"^\{env:AGENT_FLEET_OPENCODE_SECRET_[0-9A-F]{16}\}$",
        )
        self.assertIn("fake-key", config["opencode_runtime_secrets"].values())
        self.assertNotIn("fake-key", json.dumps(config["opencode_config"]))

    def test_opencode_applies_settings_to_named_provider_model(self) -> None:
        config = self._load_config(
            "opencode",
            MODEL="anthropic/test-model",
            HARBOR_TEMPERATURE="0.3",
            HARBOR_TOP_P="0.8",
            HARBOR_MAX_TOKENS="4096",
        )

        self.assertEqual(
            config["opencode_config"]["agent"]["build"],
            {"temperature": 0.3, "top_p": 0.8},
        )
        self.assertEqual(
            config["opencode_config"]["provider"]["anthropic"]["models"][
                "test-model"
            ]["limit"]["output"],
            4096,
        )
        self.assertEqual(
            config["opencode_config"].get("provider", {}).get("anthropic", {}).get(
                "options"
            ),
            {"baseURL": "https://llm.example/v1"},
        )
        self.assertNotIn("fake-key", json.dumps(config["opencode_config"]))

    def test_opencode_merges_settings_into_explicit_config(self) -> None:
        config = self._load_config(
            "opencode",
            HARBOR_TEMPERATURE="0.2",
            HARBOR_TOP_P="0.9",
            HARBOR_MAX_TOKENS="8192",
            OPENCODE_CONFIG_CONTENT=(
                '{"experimental":{"continue_loop_on_deny":true},'
                '"agent":{"build":{"temperature":0.7}}}'
            ),
        )

        self.assertTrue(
            config["opencode_config"]["experimental"]["continue_loop_on_deny"]
        )
        self.assertEqual(
            config["opencode_config"]["agent"]["build"],
            {"temperature": 0.2, "top_p": 0.9},
        )
        self.assertEqual(
            config["opencode_config"]["provider"]["custom"]["models"]["test-model"][
                "limit"
            ]["output"],
            8192,
        )

    def test_opencode_applies_sampling_to_configured_default_agent(self) -> None:
        config = self._load_config(
            "opencode",
            HARBOR_TEMPERATURE="0.2",
            HARBOR_TOP_P="0.9",
            OPENCODE_CONFIG_CONTENT=(
                '{"default_agent":"plan","agent":{'
                '"plan":{"temperature":0.7},"build":{"top_p":0.1}}}'
            ),
        )

        self.assertEqual(
            config["opencode_config"]["agent"]["plan"],
            {"temperature": 0.2, "top_p": 0.9},
        )
        self.assertEqual(
            config["opencode_config"]["agent"]["build"],
            {"top_p": 0.1},
        )

    def test_high_level_output_limit_validation_and_overrides(self) -> None:
        config = self._load_config(
            "claude-code",
            HARBOR_MAX_TOKENS="8192",
            HARBOR_MAX_NEW_TOKENS="4096",
            HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS="2048",
            HARBOR_MODEL_INFO='{"max_input_tokens":1000,"max_output_tokens":512}',
        )

        self.assertEqual(config["max_new_tokens"], "4096")
        self.assertEqual(config["model_info"]["max_output_tokens"], 512)
        self.assertEqual(config["claude_max_output_tokens"], "2048")

        result = self._run_validation(
            "claude-code",
            HARBOR_MAX_TOKENS="not-an-int",
            HARBOR_MODEL_INFO='{"max_input_tokens":1000,"max_output_tokens":512}',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HARBOR_MAX_TOKENS must be a positive integer", result.stderr)

    def test_rollout_ignores_fixed_run_generation_controls(self) -> None:
        config = self._load_config(
            "opencode",
            ROLLOUT="1",
            HARBOR_TEMPERATURE="0.2",
            HARBOR_TOP_P="0.9",
            HARBOR_MAX_TOKENS="8192",
        )

        self.assertEqual(config["llm_kwargs"]["temperature"], 1.0)
        self.assertNotIn("top_p", config["llm_kwargs"])
        self.assertEqual(config["max_new_tokens"], "65536")
        self.assertEqual(config["model_info"]["max_output_tokens"], 65536)
        self.assertEqual(config["claude_max_output_tokens"], "65536")
        self.assertNotIn("agent", config["opencode_config"])
        self.assertNotIn(
            "limit",
            config["opencode_config"]["provider"]["custom"]["models"]["test-model"],
        )

    def test_opencode_rollout_builds_named_provider_config_for_headers(self) -> None:
        config = self._load_config(
            "opencode",
            MODEL="hosted_vllm/test-model",
            ROLLOUT="1",
            HARBOR_LLM_KWARGS=(
                '{"extra_headers":{"X-Route-Key":"deployment-a"}}'
            ),
        )

        options = config["opencode_config"]["provider"]["hosted_vllm"]["options"]
        self.assertEqual(options["baseURL"], "https://llm.example/v1")
        self.assertRegex(
            options["headers"]["X-Route-Key"],
            r"^\{env:AGENT_FLEET_OPENCODE_SECRET_[0-9A-F]{16}\}$",
        )
        self.assertIn(
            "deployment-a",
            config["opencode_runtime_secrets"].values(),
        )
        self.assertNotIn("deployment-a", json.dumps(config["opencode_config"]))
        self.assertNotIn("agent", config["opencode_config"])

    def test_opencode_sanitizes_explicit_config_credentials(self) -> None:
        config = self._load_config(
            "opencode",
            MODEL="anthropic/test-model",
            OPENCODE_CONFIG_CONTENT=(
                '{"provider":{"anthropic":{"options":{'
                '"apiKey":"explicit-key","headers":{'
                '"Authorization":"Bearer explicit-token"}}}}}'
            ),
        )

        serialized_config = json.dumps(config["opencode_config"])
        self.assertNotIn("explicit-key", serialized_config)
        self.assertNotIn("explicit-token", serialized_config)
        self.assertIn(
            "explicit-key",
            config["opencode_runtime_secrets"].values(),
        )
        self.assertIn(
            "Bearer explicit-token",
            config["opencode_runtime_secrets"].values(),
        )

    def test_opencode_runtime_secrets_survive_repeated_env_loading(self) -> None:
        config = self._load_config(
            "opencode",
            load_count=2,
            MODEL="anthropic/test-model",
            OPENCODE_CONFIG_CONTENT=(
                '{"provider":{"anthropic":{"options":{'
                '"apiKey":"explicit-key","headers":{'
                '"Authorization":"Bearer explicit-token"}}}}}'
            ),
        )

        self.assertIn(
            "explicit-key",
            config["opencode_runtime_secrets"].values(),
        )
        self.assertIn(
            "Bearer explicit-token",
            config["opencode_runtime_secrets"].values(),
        )

    def test_claude_code_rejects_unsupported_sampling_settings(self) -> None:
        result = self._run_validation(
            "claude-code",
            HARBOR_TEMPERATURE="0.2",
            HARBOR_TOP_P="0.9",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Claude Code does not expose temperature or top_p controls",
            result.stderr,
        )

    def test_pi_builds_isolated_gateway_configuration(self) -> None:
        config = self._load_config(
            "pi",
            MODEL="test-model",
            BASE_URL="https://llm.example/v1/",
            HARBOR_MAX_TOKENS="8192",
            PI_THINKING_LEVEL="xhigh",
        )

        self.assertEqual(config["harbor_model"], "test-model")
        self.assertEqual(config["agent_import_path"], "pi_harbor:AgentFleetPi")
        provider = config["pi_models_config"]["providers"]["llm.example"]
        self.assertEqual(provider["baseUrl"], "https://llm.example/v1")
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$AGENT_FLEET_API_KEY")
        self.assertTrue(provider["compat"]["sendSessionAffinityHeaders"])
        self.assertEqual(
            provider["compat"]["sessionAffinityFormat"],
            "openai",
        )
        self.assertEqual(provider["models"][0]["id"], "test-model")
        self.assertEqual(provider["models"][0]["maxTokens"], 8192)
        # Thinking levels flow through to the gateway as reasoning_effort by
        # default, so max is actually emitted instead of collapsing to enabled.
        self.assertTrue(provider["compat"]["supportsReasoningEffort"])
        self.assertEqual(
            provider["models"][0]["thinkingLevelMap"]["max"],
            "max",
        )
        self.assertEqual(
            provider["models"][0]["thinkingLevelMap"]["xhigh"],
            "max",
        )
        self.assertEqual(
            provider["models"][0]["thinkingLevelMap"]["minimal"],
            "low",
        )
        self.assertEqual(
            config["pi_settings_config"],
            {
                "defaultProvider": "llm.example",
                "defaultModel": "test-model",
                "defaultThinkingLevel": "xhigh",
                "enableInstallTelemetry": False,
            },
        )
        self.assertNotIn("fake-key", json.dumps(config["pi_models_config"]))

    def test_pi_preserves_slashes_in_model_id(self) -> None:
        model = "m-20260820192358-jsrtc/deepseekv4-flash-0731"
        config = self._load_config(
            "pi",
            MODEL=model,
            BASE_URL="https://llm.example/v1/",
        )

        self.assertEqual(config["harbor_model"], model)
        provider = config["pi_models_config"]["providers"]["llm.example"]
        self.assertEqual(provider["models"][0]["id"], model)
        self.assertEqual(config["pi_settings_config"]["defaultModel"], model)

    def test_pi_can_disable_reasoning_effort_channel(self) -> None:
        config = self._load_config(
            "pi",
            MODEL="test-model",
            BASE_URL="https://llm.example/v1",
            PI_SUPPORTS_REASONING_EFFORT="0",
        )
        provider = config["pi_models_config"]["providers"]["llm.example"]
        self.assertFalse(provider["compat"]["supportsReasoningEffort"])

    def test_pi_thinking_level_map_override(self) -> None:
        config = self._load_config(
            "pi",
            MODEL="test-model",
            BASE_URL="https://llm.example/v1",
            PI_THINKING_LEVEL_MAP=json.dumps(
                {"off": None, "max": "high"}
            ),
        )
        provider = config["pi_models_config"]["providers"]["llm.example"]
        self.assertEqual(provider["models"][0]["thinkingLevelMap"]["max"], "high")

    def test_pi_rejects_unsupported_sampling_settings(self) -> None:
        result = self._run_validation(
            "pi",
            HARBOR_TEMPERATURE="0.2",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Pi does not expose temperature or top_p controls",
            result.stderr,
        )

    def test_pi_rejects_unsupported_thinking_level(self) -> None:
        result = self._run_validation(
            "pi",
            validation_function="harbor_validate_agent",
            PI_THINKING_LEVEL="ultra",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PI_THINKING_LEVEL must be off, minimal, low, medium, high, xhigh, or max",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
