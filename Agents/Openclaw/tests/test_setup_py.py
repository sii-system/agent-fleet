"""Unit tests for Agents/Openclaw/scripts/setup.py.

These complement Agents/Openclaw/tests/test_setup_mounts.py, which exercises the full
setup.sh -> setup.py wrapper end-to-end. The tests here target the pure
functions so failures point at a specific transformation."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

OPENCLAW_DIR = Path(__file__).resolve().parents[1]
SETUP_PY = OPENCLAW_DIR / "scripts" / "setup.py"
TEMPLATE_PATH = OPENCLAW_DIR / "config" / "openclaw.json.template"


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("openclaw_setup", SETUP_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("openclaw_setup", module)
    spec.loader.exec_module(module)
    return module


setup = _load_setup_module()


def _base_cfg(**overrides):
    cfg = {
        "BASE_URL": "https://api.example.com/v1",
        "API_KEY": "sk-test",
        "MODEL": "gpt-test",
        "SANDBOX_MODE": "off",
        "HEARTBEAT_EVERY": "0m",
        "EXEC_SECURITY": "deny",
        "EXEC_ASK": "always",
        "WORKSPACE_ONLY": "true",
        "OPIK_URL": "",
        "OPIK_API_KEY": "",
        "OPIK_WORKSPACE": "default",
        "OPIK_PROJECT_NAME": "",
        "NPM_CONFIG_REGISTRY": "",
        "PIP_INDEX_URL": "",
        "PIP_EXTRA_INDEX_URL": "",
        "PIP_TRUSTED_HOST": "",
        "CPU_LIMIT": "2",
        "MEM_LIMIT": "4G",
        "DOCKER_COMPOSE_READ_ONLY": "true",
        "OPENCLAW_UID": str(os.getuid()),
        "OPENCLAW_GID": str(os.getgid()),
    }
    cfg.update(overrides)
    return cfg


class BuildOpenclawConfigTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))

    def test_basic_substitution(self):
        cfg = _base_cfg(MODEL="my-model")
        result = setup.build_openclaw_config(self.template, cfg, token="t0", gw_port=18789)
        provider = result["models"]["providers"]["default"]
        self.assertEqual(provider["baseUrl"], "https://api.example.com/v1")
        self.assertEqual(provider["apiKey"], "sk-test")
        self.assertEqual(provider["models"][0]["id"], "my-model")
        self.assertEqual(provider["models"][0]["name"], "my-model")
        self.assertEqual(result["gateway"]["auth"], {"mode": "token", "token": "t0"})
        self.assertEqual(
            result["gateway"]["controlUi"]["allowedOrigins"],
            ["http://127.0.0.1:18789", "http://localhost:18789"],
        )
        self.assertEqual(result["agents"]["defaults"]["heartbeat"]["every"], "0m")
        self.assertEqual(result["plugins"]["allow"], ["openai"])
        self.assertNotIn("entries", result["plugins"])

    def test_base_url_root_gets_v1_suffix(self):
        cfg = _base_cfg(BASE_URL="https://api.example.com")
        result = setup.build_openclaw_config(self.template, cfg, token="t0", gw_port=18789)
        provider = result["models"]["providers"]["default"]
        self.assertEqual(provider["baseUrl"], "https://api.example.com/v1")

    def test_base_url_v1_is_idempotent(self):
        for given in ("https://api.example.com/v1", "https://api.example.com/v1/"):
            self.assertEqual(setup.normalize_base_url(given), "https://api.example.com/v1")

    def test_template_unchanged(self):
        snapshot = json.dumps(self.template, sort_keys=True)
        cfg = _base_cfg()
        setup.build_openclaw_config(self.template, cfg, token="t0", gw_port=18789)
        self.assertEqual(json.dumps(self.template, sort_keys=True), snapshot)

    def test_workspace_only_false(self):
        cfg = _base_cfg(WORKSPACE_ONLY="false")
        result = setup.build_openclaw_config(self.template, cfg, token="t0", gw_port=18789)
        self.assertEqual(result["tools"]["fs"]["workspaceOnly"], False)

    def test_opik_enabled(self):
        cfg = _base_cfg(OPIK_URL="https://opik.example/api/",
                        OPIK_PROJECT_NAME="proj", OPIK_API_KEY="k", OPIK_WORKSPACE="ws")
        result = setup.build_openclaw_config(self.template, cfg, token="t0", gw_port=18789)
        self.assertEqual(result["plugins"]["allow"], ["openai", "openclaw-opik-tracer"])
        self.assertEqual(result["plugins"]["load"]["paths"],
                         ["/opt/openclaw-plugins/openclaw-opik-tracer"])
        entry = result["plugins"]["entries"]["openclaw-opik-tracer"]
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["hooks"]["allowConversationAccess"])
        self.assertEqual(entry["config"]["opikUrl"], "https://opik.example/api/")
        self.assertEqual(entry["config"]["opikApiKey"], "k")
        self.assertEqual(entry["config"]["opikWorkspace"], "ws")
        self.assertEqual(entry["config"]["opikProjectName"], "proj")


class RenderComposeServiceTests(unittest.TestCase):
    def test_compose_preserves_runtime_settings_and_mirrors(self):
        cfg = _base_cfg(
            NPM_CONFIG_REGISTRY="https://registry.npmmirror.com",
            PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple",
            PIP_EXTRA_INDEX_URL="https://pypi.example.com/simple",
            PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn",
        )
        block = setup.render_compose_service(
            "openclaw-1", token_var="TOKEN_1", gw_port=18789,
            image_default="openclaw:local",
            config_dir=Path("/state/1"), workspace_dir=Path("/work/1"),
            opik_state_dir=None, openclaw_home_dir=Path("/openclaw-home/1"),
            npm_cache=Path("/cache/.npm"),
            plugin_cache=None, cfg=cfg,
        )
        service = yaml.safe_load(block)["openclaw-1"]
        expected_environment = {
            key: cfg[key] for key in
            ("NPM_CONFIG_REGISTRY", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST")
        }
        expected_environment["OPENCLAW_GATEWAY_TOKEN"] = "${TOKEN_1}"
        self.assertEqual(
            {key: service["environment"][key] for key in expected_environment},
            expected_environment,
        )
        self.assertEqual(service["ports"], ["18789:18789"])
        self.assertEqual(service["image"], "${OPENCLAW_IMAGE:-openclaw:local}")
        self.assertIs(service["read_only"], True)
        self.assertNotIn("OC_OPIK_PROCESS_TIMEOUT_S", service["environment"])


class TokenPreservationTests(unittest.TestCase):
    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            self.assertEqual(setup.load_existing_tokens(path), {})
            path.write_text(
                "TOKEN_1=abc123\nTOKEN_2=def456\nCONTAINER_NAME_PREFIX=openclaw\n",
                encoding="utf-8",
            )
            self.assertEqual(setup.load_existing_tokens(path),
                             {"TOKEN_1": "abc123", "TOKEN_2": "def456"})


class ResolveConfigTests(unittest.TestCase):
    def test_missing_credentials_point_to_private_config(self):
        cfg = setup.resolve_config({"HOME": "/h"}, [])

        with self.assertRaises(setup._ParserError) as error:
            setup.validate_required(cfg)

        self.assertIn("/config.local.env", str(error.exception))
        self.assertNotIn("/config.env ", str(error.exception))

    def test_default_sandbox_mode_is_off(self):
        env = {"BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}
        cfg = setup.resolve_config(env, [])
        self.assertEqual(cfg["SANDBOX_MODE"], "off")

    def test_absent_opik_url_disables_tracing(self):
        env = {"BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}

        cfg = setup.resolve_config(env, [])

        self.assertFalse(setup.opik_enabled(cfg))
        setup.validate_required(cfg)

    def test_opik_url_enables_tracing(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "OPIK_URL": "https://opik.example.invalid/api",
            "OPIK_PROJECT_NAME": "proj",
        }

        cfg = setup.resolve_config(env, [])

        self.assertTrue(setup.opik_enabled(cfg))
        setup.validate_required(cfg)

    def test_opik_url_without_project_name_is_rejected(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "OPIK_URL": "https://opik.example.invalid/api",
        }

        cfg = setup.resolve_config(env, [])

        with self.assertRaises(setup._ParserError):
            setup.validate_required(cfg)

    def test_retired_switches_warn_and_do_not_enable_tracing(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "TRACE_TO_OPIK": "true",
            "OPIK_PLUGIN": "enabled",
        }

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cfg = setup.resolve_config(env, [])

        self.assertFalse(setup.opik_enabled(cfg))
        self.assertIn("TRACE_TO_OPIK is no longer used", stderr.getvalue())
        self.assertIn("OPIK_PLUGIN is no longer used", stderr.getvalue())

    def test_cli_count_overrides_env(self):
        env = {"COUNT": "2", "BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}
        cfg = setup.resolve_config(env, ["5"])
        self.assertEqual(cfg["COUNT"], 5)

    def test_flags_override_env(self):
        env = {
            "BASE_URL": "u", "API_KEY": "k", "HOME": "/h",
            "SANDBOX_MODE": "non-main", "EXEC_SECURITY": "deny", "EXEC_ASK": "always",
        }
        cfg = setup.resolve_config(env, [
            "1", "--sandbox_mode", "off", "--exec_security", "full",
            "--exec_ask", "off", "--docker_compose_read_only", "false",
        ])
        self.assertEqual(cfg["SANDBOX_MODE"], "off")
        self.assertEqual(cfg["EXEC_SECURITY"], "full")
        self.assertEqual(cfg["EXEC_ASK"], "off")
        self.assertEqual(cfg["DOCKER_COMPOSE_READ_ONLY"], "false")

    def test_invalid_compose_flag(self):
        env = {"BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}
        with self.assertRaises(setup._ParserError):
            setup.resolve_config(env, ["--docker_compose_read_only", "yes"])

    def test_workspace_permission_defaults(self):
        env = {"BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}
        cfg = setup.resolve_config(env, [])
        self.assertEqual(cfg["OPENCLAW_CONFIG_CHMOD"], "a+rwX")
        self.assertEqual(cfg["OPENCLAW_CONFIG_DEFAULT_ACL"], "true")
        self.assertEqual(cfg["OPENCLAW_WORKSPACE_CHMOD"], "a+rwX")
        self.assertEqual(cfg["OPENCLAW_WORKSPACE_DEFAULT_ACL"], "true")

    def test_workspace_permission_env_overrides(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "OPENCLAW_CONFIG_CHMOD": "",
            "OPENCLAW_CONFIG_DEFAULT_ACL": "false",
            "OPENCLAW_WORKSPACE_CHMOD": "",
            "OPENCLAW_WORKSPACE_DEFAULT_ACL": "false",
        }
        cfg = setup.resolve_config(env, [])
        self.assertEqual(cfg["OPENCLAW_CONFIG_CHMOD"], "")
        self.assertEqual(cfg["OPENCLAW_CONFIG_DEFAULT_ACL"], "false")
        self.assertEqual(cfg["OPENCLAW_WORKSPACE_CHMOD"], "")
        self.assertEqual(cfg["OPENCLAW_WORKSPACE_DEFAULT_ACL"], "false")

    def test_invalid_config_default_acl_flag(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "OPENCLAW_CONFIG_DEFAULT_ACL": "yes",
        }
        with self.assertRaises(setup._ParserError):
            setup.resolve_config(env, [])

    def test_invalid_workspace_default_acl_flag(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "OPENCLAW_WORKSPACE_DEFAULT_ACL": "yes",
        }
        with self.assertRaises(setup._ParserError):
            setup.resolve_config(env, [])

    def test_mirror_env_is_resolved(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
            "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "PIP_EXTRA_INDEX_URL": "https://pypi.example.com/simple",
            "PIP_TRUSTED_HOST": "pypi.tuna.tsinghua.edu.cn",
        }
        cfg = setup.resolve_config(env, [])
        self.assertEqual(cfg["NPM_CONFIG_REGISTRY"], env["NPM_CONFIG_REGISTRY"])
        self.assertEqual(cfg["PIP_INDEX_URL"], env["PIP_INDEX_URL"])
        self.assertEqual(cfg["PIP_EXTRA_INDEX_URL"], env["PIP_EXTRA_INDEX_URL"])
        self.assertEqual(cfg["PIP_TRUSTED_HOST"], env["PIP_TRUSTED_HOST"])


if __name__ == "__main__":
    unittest.main()
