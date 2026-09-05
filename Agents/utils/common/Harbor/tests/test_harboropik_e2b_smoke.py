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
        extra_env: dict[str, str] | None = None,
        enable_pi_extensions: bool = False,
        dry_run: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            dataset_path = root_path / "dataset"
            (dataset_path / "0" / "environment").mkdir(parents=True)
            (dataset_path / "0" / "task.toml").write_text(
                "[environment]\n", encoding="utf-8"
            )
            (root_path / "queue").mkdir()
            tools_path = root_path / "bin"
            tools_path.mkdir()
            (tools_path / "uv").symlink_to("/bin/true")
            (tools_path / "uvx").symlink_to("/bin/true")
            fake_docker = tools_path / "docker"
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)
            fake_harbor = tools_path / "harbor"
            fake_harbor.write_text(
                "#!/usr/bin/env bash\nprintf 'FAKE_HARBOR_ARG=%s\\n' \"$@\"\n",
                encoding="utf-8",
            )
            fake_harbor.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "AGENT": agent,
                    "HARBOR_ENVIRONMENT_TYPE": environment_type,
                    "HARBOR_E2B_SANDBOX_TIMEOUT_SEC": "3600",
                    "HARBOR_E2B_PREBUILT_TEMPLATE": prebuilt_template,
                    "E2B_TEMPLATE": "",
                    "DATASET_NAME": "auto",
                    "HARBOR_DRY_RUN": "1" if dry_run else "0",
                    "HARBOR_OPIK_BIN": str(fake_harbor),
                    "HARBOR_CLI_BIN": str(fake_harbor),
                    "HARBOR_RUNNER_PREPARE": "0",
                    "HARBOR_SKIP_DOCKERHUB_PREFLIGHT": "1",
                    "DATASET_PATH": str(dataset_path),
                    "JOBS_ROOT": str(root_path / "jobs"),
                    "OUTPUT_ROOT": str(root_path / "output"),
                    "OUTPUT_PATH": str(root_path / "output"),
                    "RUNTIME_DIR": str(root_path / "runtime"),
                    "QUEUE_DIR": str(root_path / "queue"),
                    "HARBOR_DIRECT_BIN": "/tmp/direct-harbor",
                    "OPIK_URL": "",
                    "TRACE_TO_OPIK": "",
                    "API_KEY": "fake-api-key",
                    "BASE_URL": "https://model.example",
                    "MODEL": "test-model",
                    "HARBOR_MODEL": "test-model",
                    "HARBOR_ANTHROPIC_AUTH_TOKEN": "fake-api-key",
                    "HARBOR_LLM_KWARGS": '{"temperature":1.0}',
                    # Keep tests independent from developers' ignored local
                    # extensions. Individual cases opt in below.
                    "PI_EXTENSION_SOURCE": str(root_path / "no-pi-extensions"),
                    "PATH": f"{tools_path}:{env.get('PATH', '')}",
                }
            )
            if enable_pi_extensions:
                extensions = root_path / "pi-extensions"
                extensions.mkdir()
                (extensions / "smoke.ts").write_text(
                    "export default function () {}\n", encoding="utf-8"
                )
                env["PI_EXTENSION_SOURCE"] = str(extensions)
            if extra_env:
                env.update(extra_env)
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

    def test_pi_e2b_fails_cleanly_with_clear_message(self) -> None:
        completed = self.run_dry_run(
            "e2b", "pi", extra_env={"E2B_API_KEY": "fake-e2b-key"}
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "AGENT=pi with HARBOR_ENVIRONMENT_TYPE=e2b is unsupported",
            completed.stdout,
        )
        self.assertIn("use HARBOR_ENVIRONMENT_TYPE=docker", completed.stdout)

    def test_pi_docker_dry_run_still_builds_command(self) -> None:
        completed = self.run_dry_run("docker", "pi")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("[INFO] pi version: 0.81.1 | thinking: high", completed.stdout)
        self.assertIn("agent_import_path: pi_harbor:AgentFleetPi", completed.stdout)
        self.assertNotIn("--extra-docker-compose", completed.stdout)

    def test_pi_docker_mounts_extensions_read_only(self) -> None:
        completed = self.run_dry_run(
            "docker", "pi", enable_pi_extensions=True, dry_run=False
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn('"target": "/opt/tb-pi/extensions"', completed.stdout)
        self.assertIn('"read_only": true', completed.stdout)
        self.assertIn("PI_EXTENSION_DIR=/opt/tb-pi/extensions", completed.stdout)

    def test_benchmark_agent_env_and_native_mcp_config_reach_claude(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent_env = Path(root) / "agent.env"
            agent_env.write_text(
                "# benchmark runtime\nBROWSECOMP_MCP_URL=http://host.docker.internal:8000/mcp\nBROWSECOMP_RUN_ID=run-1\n",
                encoding="utf-8",
            )
            mcp_config = Path(root) / "mcp.json"
            mcp_config.write_text('{"mcpServers":{}}\n', encoding="utf-8")
            completed = self.run_dry_run(
                "docker",
                "claude-code",
                extra_env={
                    "HARBOR_AGENT_ENV_FILE": str(agent_env),
                    "HARBOR_MCP_CONFIG": str(mcp_config),
                },
                dry_run=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("BROWSECOMP_MCP_URL=http://host.docker.internal:8000/mcp", completed.stdout)
        self.assertIn("BROWSECOMP_RUN_ID=run-1", completed.stdout)
        self.assertIn("--mcp-config", completed.stdout)
        self.assertIn(str(mcp_config), completed.stdout)

    def test_pi_uses_benchmark_env_but_skips_unsupported_native_mcp_arg(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent_env = Path(root) / "agent.env"
            agent_env.write_text("BROWSECOMP_MCP_URL=http://retriever/mcp\n", encoding="utf-8")
            mcp_config = Path(root) / "mcp.json"
            mcp_config.write_text('{"mcpServers":{}}\n', encoding="utf-8")
            completed = self.run_dry_run(
                "docker",
                "pi",
                extra_env={"HARBOR_AGENT_ENV_FILE": str(agent_env), "HARBOR_MCP_CONFIG": str(mcp_config)},
                dry_run=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("BROWSECOMP_MCP_URL=http://retriever/mcp", completed.stdout)
        self.assertNotIn("--mcp-config", completed.stdout)

    def test_benchmark_agent_env_rejects_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent_env = Path(root) / "agent.env"
            agent_env.write_text("BAD-NAME=value\n", encoding="utf-8")
            completed = self.run_dry_run(
                "docker", "claude-code", extra_env={"HARBOR_AGENT_ENV_FILE": str(agent_env)}
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("invalid agent environment name", completed.stdout)

    def test_pi_qz_fails_cleanly_with_clear_message(self) -> None:
        completed = self.run_dry_run("qz", "pi")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "AGENT=pi with HARBOR_ENVIRONMENT_TYPE=qz is unsupported",
            completed.stdout,
        )
        self.assertIn("use HARBOR_ENVIRONMENT_TYPE=docker", completed.stdout)

    def test_oracle_startup_prepares_only_runner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                {
                    "AGENT": "claude-code",
                    "RL_AGENT": "oracle",
                    "ROLLOUT": "1",
                    "RUNTIME_DIR": root,
                    "TRACE_TO_OPIK": "",
                    "OPIK_URL": "",
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
                    "TRACE_TO_OPIK": "",
                    "OPIK_URL": "",
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
                    "TRACE_TO_OPIK": "",
                    "OPIK_URL": "",
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
