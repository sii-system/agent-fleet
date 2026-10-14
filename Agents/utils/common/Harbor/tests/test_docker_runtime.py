"""Regressions for image-owned tools and Atlas's inherited Bash entrypoint."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))
import docker_runtime


class DockerRuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.image_bin = self.root / "image-bin"
        self.tools = self.root / "offline tools $(touch injected)"
        for directory, name in ((self.image_bin, "bun"), (self.tools, "uv")):
            directory.mkdir()
            binary = directory / name
            binary.write_text(f"#!/bin/sh\nprintf '{name}-ok'\n")
            binary.chmod(0o755)
        with patch.dict(os.environ, {"HARBOR_ENVIRONMENT_TYPE": "docker"}):
            docker_runtime.patch_docker_runtime_from_env()

    def environment(self, image="example/task:latest", **kwargs):
        return DockerEnvironment(
            environment_dir=self.root,
            environment_name="task",
            session_id="task__test__env",
            trial_paths=TrialPaths(self.root / "trial"),
            task_env_config=EnvironmentConfig(docker_image=image),
            **kwargs,
        )

    def run_command(self, command, env):
        environment = self.environment()

        async def local_compose(args, **kwargs):
            effective_env = {"PATH": f"{self.image_bin}:/usr/bin:/bin"}
            for index, argument in enumerate(args):
                if argument == "-e":
                    key, _, value = args[index + 1].partition("=")
                    effective_env[key] = value
            process = await asyncio.create_subprocess_exec(
                *args[args.index("main") + 1:], env=effective_env,
                cwd=self.root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return ExecResult(
                return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode(),
            )

        environment._run_docker_compose_command = local_compose
        return asyncio.run(environment.exec(command, env=env))

    def test_verifier_can_use_image_bun_and_offline_uv(self):
        env = {docker_runtime.VERIFIER_PATH_PREPEND_ENV: str(self.tools)}
        result = self.run_command(
            'bun; uv; test -z "${HARBOR_VERIFIER_PATH_PREPEND+x}"', env,
        )
        self.assertEqual(result.return_code, 0, result.stderr)
        self.assertEqual(result.stdout, "bun-okuv-ok")
        self.assertIn(docker_runtime.VERIFIER_PATH_PREPEND_ENV, env)
        self.assertFalse((self.root / "injected").exists())

    def test_explicit_path_and_command_exit_status_are_preserved(self):
        result = self.run_command(
            'printf "%s" "$PATH"; exit 7',
            {
                docker_runtime.VERIFIER_PATH_PREPEND_ENV: str(self.tools),
                "PATH": "/explicit/task/path:/usr/bin:/bin",
            },
        )
        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, f"{self.tools}:/explicit/task/path:/usr/bin:/bin")

    def test_agent_commands_and_empty_prefix_keep_image_path(self):
        for env in (None, {docker_runtime.VERIFIER_PATH_PREPEND_ENV: ""}):
            with self.subTest(env=env):
                result = self.run_command('printf "%s" "$PATH"', env)
                self.assertEqual(result.return_code, 0, result.stderr)
                self.assertEqual(result.stdout, f"{self.image_bin}:/usr/bin:/bin")

    def test_atlas_keepalive_precedes_task_and_caller_overrides(self):
        task_compose = self.root / "docker-compose.yaml"
        task_compose.write_text("services:\n  main:\n    entrypoint: [custom-task]\n")
        caller_compose = self.root / "caller.yaml"
        caller_compose.write_text("services:\n  main:\n    command: [custom-caller]\n")
        environment = self.environment(
            "ghcr.io/scaleapi/swe-atlas@sha256:fake",
            extra_docker_compose=[caller_compose],
        )
        for use_prebuilt in (True, False):
            with self.subTest(use_prebuilt=use_prebuilt):
                environment._use_prebuilt = use_prebuilt
                paths = environment._docker_compose_paths
                main = {}
                for path in paths:
                    main.update(yaml.safe_load(path.read_text())["services"]["main"])
                self.assertEqual(main["entrypoint"], ["custom-task"])
                self.assertEqual(main["command"], ["custom-caller"])
                self.assertEqual(paths.count(docker_runtime.SWE_ATLAS_KEEPALIVE), 1)

    def test_atlas_clears_shell_entrypoint_and_keeps_harbor_command(self):
        environment = self.environment("ghcr.io/scaleapi/swe-atlas:example")
        environment._use_prebuilt = True
        main = {}
        for path in environment._docker_compose_paths:
            main.update(yaml.safe_load(path.read_text())["services"]["main"])
        self.assertEqual(main["entrypoint"], [])
        self.assertEqual(main["command"], ["sh", "-c", "sleep infinity"])

    def test_other_images_keep_their_startup_configuration(self):
        for image in (
            "ghcr.io/maxbittker/rs-agent-benchmark:v37",
            "ghcr.io/scaleapi/swe-atlas-other:example",
        ):
            with self.subTest(image=image):
                self.assertNotIn(
                    docker_runtime.SWE_ATLAS_KEEPALIVE,
                    self.environment(image)._docker_compose_paths,
                )

    def test_patch_is_idempotent_and_docker_only(self):
        for environment_type in ("docker", "qz"):
            with patch.dict(os.environ, {"HARBOR_ENVIRONMENT_TYPE": environment_type}):
                self.assertFalse(docker_runtime.patch_docker_runtime_from_env())


if __name__ == "__main__":
    unittest.main()
