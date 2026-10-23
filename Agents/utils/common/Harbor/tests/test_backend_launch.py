"""Behavior at the SDK-free Python/Shell backend launch boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from harbor_environment_adapters.core.config import BackendPlan
from harbor_environment_adapters.core.launch import launch_settings


class BackendLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"PATH": os.environ["PATH"], "HOME": str(self.root)}

    def run_helper(self, *args, **config):
        return subprocess.run(
            [sys.executable, "-S", str(HARBOR_DIR / "backend_launch.py"), *args],
            cwd=self.root, env={**self.config, **config},
            capture_output=True, text=True, check=False,
        )

    def evaluate(self, program, array):
        # Passing the program through argv avoids a second interpolation layer.
        result = subprocess.run(
            ["bash", "-c", ('set -euo pipefail; cmd=(); eval "$1"; '
                            f'printf "%s\\0" "${{{array}[@]}}"'), "bash", program],
            cwd=self.root, env=self.config, capture_output=True, check=True,
        )
        return result.stdout.decode().rstrip("\0").split("\0")

    def test_live_e2b_auth_is_separate_from_dry_preflight(self):
        config = {"HARBOR_ENVIRONMENT_TYPE": "e2b", "E2B_API_KEY": ""}
        dry = self.run_helper("validate", **config)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        ready = self.run_helper("validate", "--ready", **config)
        self.assertEqual(ready.returncode, 1)
        self.assertEqual(ready.stdout, "")
        self.assertIn("E2B_API_KEY is required", ready.stderr)

    def test_settings_are_derived_from_plan_when_backend_changes(self):
        docker = launch_settings(BackendPlan("docker", "custom:Environment"), {})
        qz = launch_settings(BackendPlan("qz", "custom:Environment"), docker)
        self.assertEqual(qz["HARBOR_BACKEND_HOST_DOCKER"], "0")
        self.assertEqual(qz["HARBOR_BACKEND_READONLY_MOUNTS"], "0")
        self.assertEqual(qz["HARBOR_BACKEND_VERIFIER_UPLOAD"], "1")
        restored = launch_settings(BackendPlan("docker", "custom:Environment"), qz)
        self.assertEqual(restored["HARBOR_BACKEND_HOST_DOCKER"], "1")
        self.assertEqual(restored["HARBOR_BACKEND_READONLY_MOUNTS"], "1")
        self.assertEqual(restored["HARBOR_BACKEND_VERIFIER_UPLOAD"], "0")

    def test_custom_spec_roundtrips_as_one_argument_without_executing_shell(self):
        spec = "custom:Env'$(touch injected)`touch injected`\nsecond-line"
        result = self.run_helper("args", HARBOR_ENVIRONMENT_TYPE="docker",
                                 HARBOR_ENVIRONMENT_SPEC=spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.evaluate(result.stdout, "cmd")
        self.assertEqual(arguments[:2], ["--env", spec])
        self.assertIn("--extra-docker-compose", arguments)
        self.assertFalse((self.root / "injected").exists())

    def test_existing_bundle_versions_precede_task_and_build_inputs(self):
        for version, main_key, ref_key in (
            (1, "main_service", "sandbox_ref"), (2, "main", "digest_ref"),
        ):
            with self.subTest(version=version):
                bundle = self.root / f"bundle-{version}.json"
                ref = "registry.example/project/task@sha256:" + "a" * 64
                bundle.write_text(json.dumps({
                    "schema_version": version, main_key: "main",
                    "services": {"main": {"image": {ref_key: ref}}},
                }))
                result = self.run_helper(
                    "image-plan", str(self.root / "unused.json"),
                    HARBOR_ENVIRONMENT_TYPE="opensandbox",
                    HARBOR_OPENSANDBOX_BUNDLE_MANIFEST=str(bundle),
                    DATASET_PATH=str(self.root / "missing"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(ref, result.stdout)
                self.assertIn("manager_args=()", result.stdout)
        broken = self.root / "broken.json"
        broken.write_text('{"schema_version":2,"main":{}}')
        result = self.run_helper("image-plan", "unused",
                                 HARBOR_ENVIRONMENT_TYPE="opensandbox",
                                 HARBOR_OPENSANDBOX_BUNDLE_MANIFEST=str(broken))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_image_manager_receives_literal_build_inputs_and_output_path(self):
        build_args = '{"LITERAL":"a\'b; $(touch injected) `touch injected`"}'
        manifest = str(self.root / "a 'quoted' bundle.json")
        result = self.run_helper(
            "image-plan", manifest, HARBOR_ENVIRONMENT_TYPE="opensandbox",
            DATASET_PATH=str(self.root), HARBOR_INCLUDE_TASKS="task",
            HARBOR_TASK_IMAGE_CLI="/builder/task_cli.py",
            HARBOR_TASK_IMAGE_BUILD_ARGS_JSON=build_args,
            HARBOR_DRY_RUN="1", HARBOR_FORCE_BUILD="true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.evaluate(result.stdout, "manager_args")
        self.assertEqual(arguments[0], "/builder/task_cli.py")
        self.assertEqual(arguments[arguments.index("--build-args-json") + 1], build_args)
        self.assertEqual(arguments[arguments.index("--bundle-manifest-output") + 1], manifest)
        self.assertIn("--force", arguments)
        self.assertIn("--dry-run", arguments)
        self.assertFalse((self.root / "injected").exists())

    def test_native_and_failed_manager_results_emit_no_partial_assignments(self):
        native = self.run_helper(
            "image-plan", "unused.json", HARBOR_ENVIRONMENT_TYPE="opensandbox",
            HARBOR_NATIVE_CONCURRENCY="1", HARBOR_FORCE_BUILD="true",
        )
        self.assertEqual(native.returncode, 1)
        self.assertEqual(native.stdout, "")
        failed = self.run_helper("image-result", "registry.example/image", "missing.json")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertIn("did not write Bundle Manifest", failed.stderr)

    def test_invalid_task_and_config_do_not_expose_credentials(self):
        task = self.root / "task"
        task.mkdir()
        marker = "fake-sensitive-value"
        (task / "task.toml").write_text(f'[environment\ndocker_image = "{marker}"')
        result = self.run_helper(
            "validate", HARBOR_ENVIRONMENT_TYPE="opensandbox",
            YICLOUD_PUBLIC_KEY=marker, YICLOUD_SECRET_KEY=marker,
            YICLOUD_PROJECT_NAME="project", YICLOUD_HARBOR_HOST="registry.example",
            YICLOUD_SANDBOX_ENVIRONMENT_ID="env", DATASET_PATH=str(self.root),
            HARBOR_INCLUDE_TASKS="task",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(marker, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
