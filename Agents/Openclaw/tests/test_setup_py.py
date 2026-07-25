"""Unit tests for Agents/Openclaw/scripts/setup.py.

These complement Agents/Openclaw/tests/test_setup_mounts.py, which exercises the full
setup.sh -> setup.py wrapper end-to-end. The tests here target the pure
functions so failures point at a specific transformation."""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

OPENCLAW_DIR = Path(__file__).resolve().parents[1]
SETUP_PY = OPENCLAW_DIR / "scripts" / "setup.py"
TEMPLATE_PATH = OPENCLAW_DIR / "config" / "openclaw.json.template"
ANSIBLE_FLEET_TEMPLATE_PATH = (
    OPENCLAW_DIR / "config" / "ansible" / "templates" / "fleet.env.j2"
)


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("openclaw_setup", SETUP_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("openclaw_setup", module)
    spec.loader.exec_module(module)
    return module


setup = _load_setup_module()


class AnsibleFleetTemplateTests(unittest.TestCase):
    def test_trace_switch_is_normalized_in_ansible_template(self):
        trace_lines = [
            line
            for line in ANSIBLE_FLEET_TEMPLATE_PATH.read_text(encoding="utf-8").splitlines()
            if line.startswith("TRACE_TO_OPIK=")
        ]

        self.assertEqual(
            trace_lines,
            [
                (
                    "TRACE_TO_OPIK={{ 'false' if (trace_to_opik | default(true) | string | lower) "
                    "in ['false', '0'] else 'true' }}"
                ),
            ],
        )


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
        "TRACE_TO_OPIK": "true",
        "OPIK_PLUGIN": "disabled",
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
        cfg = _base_cfg(OPIK_PLUGIN="enabled", OPIK_URL="https://opik.example/api/",
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

    def test_special_characters_roundtrip(self):
        cfg = _base_cfg(
            BASE_URL='https://example/v1?n="q"&p=a\\b',
            API_KEY='k"with\\slash|pipe',
            MODEL='m"o\\d|e',
        )
        result = setup.build_openclaw_config(self.template, cfg, token="t", gw_port=18789)
        provider = result["models"]["providers"]["default"]
        self.assertEqual(provider["baseUrl"], cfg["BASE_URL"])
        self.assertEqual(provider["apiKey"], cfg["API_KEY"])
        self.assertEqual(provider["models"][0]["id"], cfg["MODEL"])


class RenderComposeServiceTests(unittest.TestCase):
    def test_basic_service_block(self):
        cfg = _base_cfg()
        block = setup.render_compose_service(
            "openclaw-1", token_var="TOKEN_1", gw_port=18789,
            image_default="openclaw:local",
            config_dir=Path("/state/1"), workspace_dir=Path("/work/1"),
            opik_state_dir=None, openclaw_home_dir=Path("/openclaw-home/1"),
            npm_cache=Path("/cache/.npm"),
            plugin_cache=None, cfg=cfg,
        )
        self.assertIn("  openclaw-1:", block)
        self.assertIn("image: ${OPENCLAW_IMAGE:-openclaw:local}", block)
        self.assertIn("/state/1:/home/node/openclaw-state", block)
        self.assertIn("/work/1:/home/node/workspace", block)
        self.assertIn("/openclaw-home/1:/home/node/.openclaw", block)
        self.assertIn(f'    user: "{os.getuid()}:{os.getgid()}"', block)
        self.assertIn('OPENCLAW_GATEWAY_TOKEN: "${TOKEN_1}"', block)
        self.assertIn('"18789:18789"', block)
        self.assertIn("read_only: true", block)
        self.assertNotIn("OC_OPIK_PROCESS_TIMEOUT_S", block)
        self.assertNotIn(":/home/node/.openclaw/state", block)
        self.assertNotIn("/opt/plugin-cache", block)

    def test_opik_block_has_state_mount_and_timeout(self):
        cfg = _base_cfg(OPIK_PLUGIN="enabled")
        block = setup.render_compose_service(
            "openclaw-2", token_var="TOKEN_2", gw_port=18809,
            image_default="openclaw:local-opik",
            config_dir=Path("/state/2"), workspace_dir=Path("/work/2"),
            opik_state_dir=Path("/state/2/opik-state"),
            openclaw_home_dir=Path("/openclaw-home/2"),
            npm_cache=Path("/cache/.npm"), plugin_cache=None, cfg=cfg,
        )
        self.assertIn('OC_OPIK_PROCESS_TIMEOUT_S: "60"', block)
        self.assertIn("/state/2/opik-state:/home/node/.openclaw/state", block)
        self.assertIn("/openclaw-home/2:/home/node/.openclaw", block)
        self.assertIn("openclaw:local-opik", block)

    def test_mirror_environment_is_emitted_when_configured(self):
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
        self.assertIn('NPM_CONFIG_REGISTRY: "https://registry.npmmirror.com"', block)
        self.assertIn('PIP_INDEX_URL: "https://pypi.tuna.tsinghua.edu.cn/simple"', block)
        self.assertIn('PIP_EXTRA_INDEX_URL: "https://pypi.example.com/simple"', block)
        self.assertIn('PIP_TRUSTED_HOST: "pypi.tuna.tsinghua.edu.cn"', block)


class TokenPreservationTests(unittest.TestCase):
    def test_round_trip(self):
        with self._tmpfile() as path:
            path.write_text(
                "TOKEN_1=abc123\n"
                "TOKEN_2=def456\n"
                "CONTAINER_NAME_PREFIX=openclaw\n",
                encoding="utf-8",
            )
            tokens = setup.load_existing_tokens(path)
            self.assertEqual(tokens, {"TOKEN_1": "abc123", "TOKEN_2": "def456"})

    def test_missing_file(self):
        with self._tmpfile() as path:
            path.unlink()
            self.assertEqual(setup.load_existing_tokens(path), {})

    def _tmpfile(self):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            with tempfile.NamedTemporaryFile(delete=False) as f:
                p = Path(f.name)
            try:
                yield p
            finally:
                if p.exists():
                    p.unlink()
        return ctx()


class ResolveConfigTests(unittest.TestCase):
    def test_default_sandbox_mode_is_off(self):
        env = {"BASE_URL": "u", "API_KEY": "k", "HOME": "/h"}
        cfg = setup.resolve_config(env, [])
        self.assertEqual(cfg["SANDBOX_MODE"], "off")

    def test_trace_off_overrides_stale_enabled_opik_plugin(self):
        env = {
            "BASE_URL": "u",
            "API_KEY": "k",
            "HOME": "/h",
            "TRACE_TO_OPIK": "false",
            "OPIK_PLUGIN": "enabled",
        }

        cfg = setup.resolve_config(env, [])

        self.assertEqual(cfg["TRACE_TO_OPIK"], "false")
        self.assertEqual(cfg["OPIK_PLUGIN"], "disabled")
        setup.validate_required(cfg)

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
