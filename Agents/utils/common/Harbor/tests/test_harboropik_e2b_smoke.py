"""Dry-run contract for the agent-fleet Harbor-to-E2B command handoff."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "harboropik.sh"
ENV_SCRIPT = Path(__file__).parents[1] / "env.sh"


class HarborOpikE2BSmokeTest(unittest.TestCase):
    def test_web_mcp_delivery_is_shared_by_all_supported_harnesses(self):
        with tempfile.TemporaryDirectory() as cache:
            builder = SCRIPT.parent.parent / "mcp/build.py"
            built = subprocess.run([sys.executable, str(builder), cache],
                                   check=True, capture_output=True, text=True)
            artifact = built.stdout.strip()
            for agent in ("claude-code", "opencode", "pi"):
                with self.subTest(agent=agent):
                    result = self.run_dry_run("docker", agent, dry_run=agent == "opencode", extra_env={
                        "HARBOR_CC_WEB_MCP_ENABLED": "1", "EXA_API_KEY": "fake-exa-secret",
                        "LOCAL_WHEEL_DIR": cache,
                    })
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn("CC_WEB_MCP_PATH=/opt/agent-fleet/exa_web_mcp.py", result.stdout)
                    if agent == "opencode":
                        self.assertNotIn("fake-exa-secret", result.stdout)
                        self.assertNotIn("EXA_API_KEY=", result.stdout)
                    else:
                        self.assertIn("EXA_API_KEY=fake-exa-secret", result.stdout)
                    self.assertIn(artifact, result.stdout)

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
                    "HARBOR_OPIK_PYTHON": str(fake_harbor),
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
        self.assertNotIn("--allow-agent-host", completed.stdout)
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

    def test_opencode_requires_the_selected_provider_endpoint(self) -> None:
        for base_url in (None, "https://actual.example:8443/v1"):
            with self.subTest(base_url=base_url):
                options = {"baseURL": base_url} if base_url else {}
                completed = self.run_dry_run("docker", "opencode", dry_run=False, extra_env={
                    "HARBOR_MODEL": "minimax/model",
                    "OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {"minimax": {"options": options}}}),
                })
                self.assertEqual(completed.returncode == 0, bool(base_url), completed.stdout)
                if base_url:
                    self.assertIn("FAKE_HARBOR_ARG=--allow-agent-host\nFAKE_HARBOR_ARG=actual.example",
                                  completed.stdout)
                else:
                    self.assertIn("Set options.baseURL for the selected OpenCode provider", completed.stdout)
                    self.assertNotIn("FAKE_HARBOR_ARG=run", completed.stdout)
                self.assertNotIn("FAKE_HARBOR_ARG=--allow-agent-host\nFAKE_HARBOR_ARG=model.example",
                                 completed.stdout)

    def test_pi_exception_uses_model_provider_with_conflicting_provider_setting(self) -> None:
        completed = self.run_dry_run("docker", "pi", dry_run=False, extra_env={
            "HARBOR_MODEL": "selected/test-model", "PI_PROVIDER": "unused",
            "PI_MODELS_CONFIG": '{"providers":{'
                '"selected":{"baseUrl":"https://selected.example:8443/v1"},'
                '"unused":{"baseUrl":"https://unused.example/v1"}}}',
        })
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("FAKE_HARBOR_ARG=--allow-agent-host\nFAKE_HARBOR_ARG=selected.example",
                      completed.stdout)
        for name in ("NO_PROXY", "no_proxy"):
            value = next(line.split("=", 2)[2] for line in completed.stdout.splitlines()
                         if line.startswith(f"FAKE_HARBOR_ARG={name}="))
            self.assertIn("selected.example", value.split(","))
            self.assertNotIn("unused.example", value.split(","))
            self.assertNotIn("model.example", value.split(","))

    def test_rollout_request_endpoints_do_not_add_agent_network_exceptions(self) -> None:
        for agent in ("claude-code", "opencode"):
            with self.subTest(agent=agent):
                completed = self.run_dry_run("docker", agent, dry_run=False, extra_env={
                    "ROLLOUT": "1", "RL_AGENT": agent,
                    "RL_API_BASE": "https://caller.example/v1",
                    "BASE_URL": "https://caller.example",
                    "HARBOR_ANTHROPIC_BASE_URL": "https://caller.example",
                })
                self.assertEqual(completed.returncode, 0, completed.stdout)
                self.assertIn("FAKE_HARBOR_ARG=run", completed.stdout)
                self.assertNotIn("--allow-agent-host", completed.stdout)

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
