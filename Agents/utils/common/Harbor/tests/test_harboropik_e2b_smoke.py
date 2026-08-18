"""Dry-run contract for the agent-fleet Harbor-to-E2B command handoff."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "harboropik.sh"
ENV_SCRIPT = Path(__file__).parents[1] / "env.sh"


class HarborOpikE2BSmokeTest(unittest.TestCase):
    def run_dry_run(
        self,
        environment_type: str,
        agent: str = "oracle",
        prebuilt_template: str = "",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tools_path = root_path / "bin"
            tools_path.mkdir()
            (tools_path / "uv").symlink_to("/bin/true")
            (tools_path / "uvx").symlink_to("/bin/true")
            env = os.environ.copy()
            env.update(
                {
                    "AGENT": agent,
                    "HARBOR_ENVIRONMENT_TYPE": environment_type,
                    "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "3600",
                    "HARBOR_E2B_PREBUILT_TEMPLATE": prebuilt_template,
                    "E2B_TEMPLATE": "",
                    "HARBOR_DRY_RUN": "1",
                    "DATASET_PATH": str(root_path / "dataset"),
                    "JOBS_ROOT": str(root_path / "jobs"),
                    "OUTPUT_ROOT": str(root_path / "output"),
                    "OUTPUT_PATH": str(root_path / "output"),
                    "RUNTIME_DIR": str(root_path / "runtime"),
                    "QUEUE_DIR": str(root_path / "queue"),
                    "HARBOR_DIRECT_BIN": "/tmp/direct-harbor",
                    "TRACE_TO_OPIK": "false",
                    "API_KEY": "fake-api-key",
                    "BASE_URL": "https://model.example",
                    "MODEL": "test-model",
                    "HARBOR_MODEL": "test-model",
                    "HARBOR_ANTHROPIC_AUTH_TOKEN": "fake-api-key",
                    "HARBOR_LLM_KWARGS": '{"temperature":1.0}',
                    "PATH": f"{tools_path}:{env.get('PATH', '')}",
                }
            )
            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return completed

    def test_oracle_e2b_command_skips_docker_and_bind_mounts(self) -> None:
        completed = self.run_dry_run("e2b")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("--env e2b", completed.stdout)
        self.assertIn("-a oracle", completed.stdout)
        self.assertIn("harbor run", completed.stdout)
        self.assertNotIn("opik harbor run", completed.stdout.lower())
        self.assertNotIn("--mounts-json", completed.stdout)
        self.assertNotIn("--extra-docker-compose", completed.stdout)
        self.assertNotIn("Docker daemon", completed.stdout)
        self.assertNotIn("Docker Hub", completed.stdout)
        self.assertIn("E2B verifier uv tools will be uploaded", completed.stdout)
        self.assertIn(
            "HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin", completed.stdout
        )

    def test_oracle_e2b_uses_host_configured_prebuilt_environment(self) -> None:
        completed = self.run_dry_run("e2b", prebuilt_template="template-test")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(
            "--env e2b_prebuilt:PrebuiltE2BEnvironment", completed.stdout
        )
        self.assertNotIn("template-test", completed.stdout)

    def test_claude_e2b_command_skips_docker_compose_overlay(self) -> None:
        completed = self.run_dry_run("e2b", "claude-code")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("[INFO] environment: e2b", completed.stdout)
        self.assertIn("skip unprivileged Docker Compose overlay", completed.stdout)
        self.assertNotIn("--extra-docker-compose", completed.stdout)

    def test_oracle_startup_prepares_only_runner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                {
                    "AGENT": "claude-code",
                    "RL_AGENT": "oracle",
                    "ROLLOUT": "1",
                    "RUNTIME_DIR": root,
                }
            )
            command = (
                f". {ENV_SCRIPT!s}; "
                "harbor_validate_runner_cli() { printf runner-only; }; "
                "harbor_prepare_or_select_wheels() { printf unexpected-wheels; return 1; }; "
                "harbor_prepare_agent_runtime"
            )
            completed = subprocess.run(
                ["bash", "-c", command],
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stdout, "runner-only")

    def test_real_agent_e2b_startup_skips_runner_local_dependency_cache(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                {
                    "AGENT": "claude-code",
                    "RL_AGENT": "claude-code",
                    "ROLLOUT": "1",
                    "RL_ENVIRONMENT_TYPE": "e2b",
                    "HARBOR_ENVIRONMENT_TYPE": "e2b",
                    "RUNTIME_DIR": root,
                }
            )
            command = (
                f". {ENV_SCRIPT!s}; "
                "harbor_validate_runner_cli() { printf runner-only; }; "
                "harbor_prepare_or_select_wheels() { printf unexpected-wheels; return 1; }; "
                "harbor_prepare_agent_runtime"
            )
            completed = subprocess.run(
                ["bash", "-c", command],
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stdout, "runner-only")

    def test_rollout_dataset_root_inherits_host_dataset_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                {
                    "ROLLOUT": "1",
                    "DATASET_NAME": "auto",
                    "DATASET_PATH": root,
                    "OUTPUT_PATH": str(Path(root) / "output"),
                }
            )
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'. {ENV_SCRIPT!s}; printf "%s" "$RL_DATASET_ROOT"',
                ],
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stdout, root)


if __name__ == "__main__":
    unittest.main()
