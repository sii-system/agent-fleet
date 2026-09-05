from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BENCHMARK = Path(__file__).resolve().parents[1]
RUNNER = BENCHMARK / "scripts" / "run.sh"
LAUNCHER = BENCHMARK / "mcp" / "launcher.py"
sys.path.insert(0, str(BENCHMARK / "scripts"))
from common import REPO_ROOT, default_source_root  # noqa: E402


class RunScriptTest(unittest.TestCase):
    def test_default_source_is_the_agent_fleet_submodule(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                default_source_root(),
                (REPO_ROOT / "third_party" / "BrowseComp-Plus").resolve(),
            )

    def fixture_env(self, root: Path) -> tuple[dict[str, str], Path]:
        source = root / "BrowseComp-Plus"
        (source / "searcher").mkdir(parents=True)
        (source / "searcher" / "mcp_server.py").write_text("# fixture\n", encoding="utf-8")
        (source / "data").mkdir()
        gold = source / "data" / "browsecomp_plus_decrypted.jsonl"
        gold.write_text(json.dumps({"query_id": "q1", "query": "Fixture question", "answer": "Private answer"}) + "\n", encoding="utf-8")
        index = source / "index.pkl"
        index.write_bytes(b"fixture")
        env = os.environ.copy()
        env.update(
            {
                "BROWSECOMP_SOURCE_ROOT": str(source),
                "BROWSECOMP_GROUND_TRUTH": str(gold),
                "BROWSECOMP_CACHE_ROOT": str(root / "cache"),
                "BROWSECOMP_INDEX_PATH": str(index),
                "OUTPUT_PATH": str(root / "output"),
                "RUN_ID": "fixture-run",
                "BROWSECOMP_SKIP_BOOTSTRAP": "1",
                "BROWSECOMP_SKIP_MCP_START": "1",
                "BROWSECOMP_MCP_HOST_IP": "10.192.0.1",
                "BASE_URL": "https://gateway.example.invalid/v1",
                "AGENT_FLEET_CONFIG_LOADED_ROOT": str(REPO_ROOT),
            }
        )
        return env, source

    def test_prepare_only_materializes_without_gold(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            env, _ = self.fixture_env(root_path)
            completed = subprocess.run(
                ["bash", str(RUNNER), "--prepare-only", "--task", "q1", "--agent", "pi"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            task_root = root_path / "output" / "browsecomp-plus" / "tasks" / "q1"
            rendered = "\n".join(path.read_text(errors="ignore") for path in task_root.rglob("*") if path.is_file())
            self.assertIn("Fixture question", rendered)
            self.assertNotIn("Private answer", rendered)
            task_config = (task_root / "task.toml").read_text(encoding="utf-8")
            self.assertIn('network_mode = "no-network"', task_config)
            self.assertIn('"gateway.example.invalid"', task_config)
            self.assertIn('"10.192.0.1"', task_config)
            self.assertNotIn('"host.docker.internal"', task_config)
            self.assertNotIn("allow_internet", task_config)
            runtime = root_path / "output" / "browsecomp-plus" / "runtime"
            self.assertTrue((runtime / "agent.env").is_file())
            self.assertTrue((runtime / "mcp.json").is_file())
            self.assertTrue((runtime / "pi-extensions" / "browsecomp_mcp.ts").is_file())
            self.assertTrue(
                (runtime / "pi-extensions" / "auto_continue_after_compaction.ts").is_file()
            )

    def test_rejects_remote_sandbox_for_local_retriever(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env, _ = self.fixture_env(Path(root))
            env["BROWSECOMP_ENVIRONMENT_TYPE"] = "qz"
            completed = subprocess.run(
                ["bash", str(RUNNER), "--dry-run", "--agent", "pi"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("requires BROWSECOMP_ENVIRONMENT_TYPE=docker", completed.stderr)

    def test_direct_runner_loads_repository_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            copied_runner = root_path / "Tasks" / "BrowseComp-Plus" / "scripts" / "run.sh"
            copied_runner.parent.mkdir(parents=True)
            (root_path / "scripts").mkdir()
            for source, destination in (
                (RUNNER, copied_runner),
                (REPO_ROOT / "scripts" / "prerequisites.sh", root_path / "scripts" / "prerequisites.sh"),
                (REPO_ROOT / "scripts" / "config_loader.sh", root_path / "scripts" / "config_loader.sh"),
                (REPO_ROOT / "scripts" / "script_utils.py", root_path / "scripts" / "script_utils.py"),
            ):
                shutil.copy2(source, destination)
            (root_path / "config.env").write_text(
                "AGENT=pi\nTOTAL_WORKERS=3\nBASE_URL=https://saved-gateway.example.invalid/v1\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            for name in (
                "AGENT",
                "TOTAL_WORKERS",
                "BASE_URL",
                "HARBOR_ANTHROPIC_BASE_URL",
                "AGENT_FLEET_CONFIG_LOADED_ROOT",
            ):
                env.pop(name, None)
            env.update(
                {
                    "AGENT_FLEET_PATHS_FILE": str(root_path / "missing-paths.env"),
                    "BROWSECOMP_SOURCE_ROOT": str(root_path / "source"),
                    "BROWSECOMP_CACHE_ROOT": str(root_path / "cache"),
                    "BROWSECOMP_GROUND_TRUTH": str(root_path / "gold.jsonl"),
                    "BROWSECOMP_INDEX_PATH": str(root_path / "index.pkl"),
                    "BROWSECOMP_MCP_PORT": "8123",
                    "BROWSECOMP_MCP_PUBLIC_URL": "http://10.192.0.1:8123/mcp",
                }
            )

            completed = subprocess.run(
                ["/bin/bash", str(copied_runner), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("agent=pi workers=3", completed.stdout)
            self.assertIn("--allowed-host saved-gateway.example.invalid", completed.stdout)

    def test_named_run_rejects_changed_selection_before_materializing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            env, _ = self.fixture_env(root_path)
            output = root_path / "output"
            output.mkdir()
            (output / "tasks.txt").write_text("different-task\n", encoding="utf-8")

            completed = subprocess.run(
                ["/bin/bash", str(RUNNER), "--prepare-only", "--task", "q1"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("set RESET_RUN=1 or use a new RUN_ID", completed.stderr)
            self.assertFalse(
                (output / "browsecomp-plus" / "task-manifest.json").exists()
            )

    def test_forwards_materialized_selection_to_harbor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            env, _ = self.fixture_env(root_path)
            env["BROWSECOMP_JUDGE_MODE"] = "none"
            capture = root_path / "harbor-env.txt"
            wrapper_dir = root_path / "bin"
            wrapper_dir.mkdir()
            bash_wrapper = wrapper_dir / "bash"
            bash_wrapper.write_text(
                """#!/bin/sh
case "$1" in
  */Agents/utils/common/Harbor/start.sh)
    printf 'FLEET_TASKS=%s\\nTASK_SOURCE_FILE=%s\\n' "$FLEET_TASKS" "$TASK_SOURCE_FILE" > "$BROWSECOMP_HARBOR_CAPTURE"
    exit 0
    ;;
esac
exec /bin/bash "$@"
""",
                encoding="utf-8",
            )
            bash_wrapper.chmod(0o755)
            env["PATH"] = f"{wrapper_dir}:{env['PATH']}"
            env["BROWSECOMP_HARBOR_CAPTURE"] = str(capture)

            completed = subprocess.run(
                ["/bin/bash", str(RUNNER), "--task", "q1", "--agent", "pi"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            captured = capture.read_text(encoding="utf-8")
            self.assertIn("FLEET_TASKS=q1", captured)
            self.assertIn("browsecomp-plus/tasks.txt", captured)

    def test_dry_run_bypasses_proxy_for_host_services(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env, _ = self.fixture_env(Path(root))
            env["NO_PROXY"] = "existing.example"
            env["BASE_URL"] = "https://gateway.example.invalid/v1"
            completed = subprocess.run(
                ["bash", str(RUNNER), "--dry-run", "--agent", "pi"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("existing.example", completed.stdout)
            self.assertIn("10.192.0.1", completed.stdout)
            self.assertNotIn("host.docker.internal", completed.stdout)
            self.assertIn("gateway.example.invalid", completed.stdout)
            self.assertIn("--allowed-host gateway.example.invalid", completed.stdout)
            self.assertIn("--allowed-host 10.192.0.1", completed.stdout)

    def test_unprivileged_overlay_is_compatible_with_egress_sidecar(self) -> None:
        overlay = (
            REPO_ROOT
            / "Agents"
            / "utils"
            / "common"
            / "Harbor"
            / "overlays"
            / "unprivileged-task.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("extra_hosts", overlay)
        self.assertNotIn("host-gateway", overlay)

    def test_launcher_command_and_health_do_not_import_upstream_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            env, source = self.fixture_env(root_path)
            command = subprocess.run(
                ["python3", str(LAUNCHER), "command", "--source-root", str(source), "--state-dir", str(root_path / "state")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr)
            resolved = json.loads(command.stdout)
            self.assertTrue(any(value.endswith("/mcp/server.py") for value in resolved))
            self.assertNotIn(str(source / "searcher" / "mcp_server.py"), resolved)
            self.assertIn("c54f2e6e80b2d7b7de06f51cec4959f6b3e03418", resolved)
            self.assertIn("1b854ae04817320c2a088c0ff9830ffcb92ca079", resolved)
            env.pop("BROWSECOMP_INDEX_PATH")
            health = subprocess.run(
                ["python3", str(LAUNCHER), "health", "--source-root", str(source), "--state-dir", str(root_path / "state")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(health.returncode, 1)
            self.assertFalse(json.loads(health.stdout)["healthy"])


if __name__ == "__main__":
    unittest.main()
