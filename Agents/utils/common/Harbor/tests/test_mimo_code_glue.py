from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
MIMO_DIR = HARBOR_DIR / "model-fusion" / "mimo-code"


class MimoCodeProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.capture = self.root / "args"
        self.wheel = self.root / "router.whl"
        self.config = self.root / "router.json"
        self.wheel.write_bytes(b"fixture")
        self.config.write_text("{}\n", encoding="utf-8")
        self.real_opik = self.root / "opik"
        self.real_opik.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "pathlib.Path(os.environ['CAPTURE']).write_text("
            "'\\n'.join(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.real_opik.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "CAPTURE": str(self.capture),
                "MIMO_CODE_REAL_HARBOR_OPIK_BIN": str(self.real_opik),
                "MIMO_ROUTER_WHEEL": str(self.wheel),
                "MIMO_ROUTER_CONFIG": str(self.config),
                "MIMO_ROUTER_VERSION": "0.2.0",
                "MIMO_ROUTER_WHEEL_MOUNT_PATH": "/opt/router.whl",
                "MIMO_ROUTER_CONFIG_MOUNT_PATH": "/opt/router.json",
                "HARBOR_ANTHROPIC_BASE_URL": "https://gateway.internal",
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_env_can_be_sourced_without_repo_root(self) -> None:
        env = {**os.environ}
        env.pop("REPO_ROOT", None)
        env.pop("FUSION_ROUTER_DIR", None)
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {MIMO_DIR / 'env.sh'}; printf '%s' \"$REPO_ROOT\"",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(HARBOR_DIR.parents[3]))

    def test_harbor_run_injects_mounts_and_runtime_contract(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(MIMO_DIR / "harboropik.sh"),
                "harbor",
                "run",
                "--dataset",
                "auto",
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_text(encoding="utf-8").splitlines()
        mounts = json.loads(args[args.index("--mounts-json") + 1])
        self.assertEqual(
            {(item["source"], item["target"]) for item in mounts},
            {
                (str(self.wheel), "/opt/router.whl"),
                (str(self.config), "/opt/router.json"),
            },
        )
        agent_env = {
            args[index + 1]
            for index, value in enumerate(args[:-1])
            if value == "--ae"
        }
        self.assertIn("MIMO_ROUTER_ENABLED=1", agent_env)
        self.assertIn("MIMO_ROUTER_PIPELINE=mimo_max", agent_env)
        self.assertIn("MIMO_ROUTER_VERSION=0.2.0", agent_env)
        self.assertIn("NO_PROXY=gateway.internal", agent_env)
        self.assertIn("no_proxy=gateway.internal", agent_env)

    def test_help_is_forwarded_without_mimo_contract(self) -> None:
        result = subprocess.run(
            ["bash", str(MIMO_DIR / "harboropik.sh"), "harbor", "run", "--help"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.capture.read_text(encoding="utf-8").splitlines(),
            ["harbor", "run", "--help"],
        )

    def test_render_only_validates_contract_without_executing_real_cli(self) -> None:
        env = {**self.env, "MODEL_FUSION_PROXY_RENDER_ONLY": "1"}
        result = subprocess.run(
            [
                "bash",
                str(MIMO_DIR / "harboropik.sh"),
                "harbor",
                "run",
                "--dataset",
                "terminal-bench/terminal-bench-2-1",
                "-i",
                "terminal-bench/fix-git",
                "--ae",
                "ANTHROPIC_API_KEY=do-not-print-this",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertIn("MIMO_ROUTER_ENABLED=1", result.stdout)
        self.assertIn("terminal-bench/fix-git", result.stdout)
        self.assertIn("--mounts-json", result.stdout)
        self.assertNotIn("do-not-print-this", result.stdout)
        self.assertIn("ANTHROPIC_API_KEY", result.stdout)
        self.assertIn("redacted", result.stdout)


class MimoCodeClaudeOverlayTest(unittest.TestCase):
    def test_wraps_only_enabled_claude_stream_commands(self) -> None:
        path = MIMO_DIR / "sitecustomize.py"
        spec = importlib.util.spec_from_file_location("mimo_sitecustomize_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        command_prefix = (
            'export PATH="$HOME/.local/bin:$PATH"; '
            "ANTHROPIC_AUTH_TOKEN=fixture-token "
            "ANTHROPIC_BASE_URL=https://gateway.internal "
        )
        command = command_prefix + (
            "claude --verbose --output-format=stream-json --print -- task"
            " 2>&1 </dev/null | tee /logs/agent/claude-code.txt"
        )
        extra_env = {
            "MIMO_ROUTER_ENABLED": "1",
            "MIMO_ROUTER_VERSION": "0.2.0",
            "MIMO_ROUTER_PIPELINE": "mimo_max",
            "MIMO_ROUTER_WHEEL_PATH": "/opt/router.whl",
            "MIMO_ROUTER_CONFIG_PATH": "/opt/router.json",
            "HARBOR_TASK_ID": "fixture-task",
        }
        wrapped = module._wrap_claude_command(
            command, "fixture prompt", extra_env, "/workspace/task repo"
        )
        self.assertIn("sii_fusion_router.cli claude", wrapped)
        self.assertIn("--pipeline mimo_max", wrapped)
        self.assertIn("--task-id fixture-task", wrapped)
        self.assertIn("--workspace '/workspace/task repo'", wrapped)
        self.assertIn(command_prefix + "PYTHONPATH=", wrapped)
        self.assertNotIn("--workspace /app", wrapped)
        self.assertNotIn('--upstream "$ANTHROPIC_BASE_URL"', wrapped)
        self.assertGreater(wrapped.index(command_prefix), wrapped.index("ZipFile"))
        syntax = subprocess.run(
            ["bash", "-n", "-c", wrapped],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertEqual(
            module._wrap_claude_command(command, "fixture prompt", {}),
            command,
        )

        setup_command = "mkdir -p $CLAUDE_CONFIG_DIR/debug"
        self.assertEqual(
            module._wrap_claude_command(setup_command, "fixture prompt", extra_env),
            setup_command,
        )
        with self.assertRaisesRegex(RuntimeError, "command shape is unsupported"):
            module._wrap_claude_command(
                "claude --print -- task", "fixture prompt", extra_env
            )


if __name__ == "__main__":
    unittest.main()
