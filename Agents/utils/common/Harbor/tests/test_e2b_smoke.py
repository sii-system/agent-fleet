"""Acceptance checks for the native E2B canary, without cloud access."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "e2b_smoke.py"


class E2BSmokeLauncherTest(unittest.TestCase):
    def run_launcher(self, *args, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update({
                "PATH": f"{Path(sys.executable).parent}:{env['PATH']}",
                "E2B_API_KEY": "",
                "E2B_DEBUG": "false",
                "OUTPUT_PATH": str(Path(directory) / "run"),
                "HARBOR_RUNNER_PREPARE": "0",
                "HARBOR_E2B_PREBUILT_TEMPLATE": "old-template",
                "E2B_TEMPLATE": "old-template",
                "HARBOR_ENVIRONMENT_SPEC": "qz_e2b_sandbox:QzSandboxEnvironment",
                "RL_ENVIRONMENT_TYPE": "qz",
            })
            env.update(overrides)
            return subprocess.run(
                ["bash", str(SCRIPT.with_name("run_e2b_smoke.sh")), *args],
                env=env, capture_output=True, text=True, check=False,
            )

    def test_preview_uses_native_e2b_without_credentials_for_both_providers(self):
        for transport in ({}, {
            "E2B_API_URL": "https://api.example.invalid",
            "E2B_SANDBOX_URL": "https://gateway.example.invalid",
        }):
            with self.subTest(transport=transport):
                result = self.run_launcher("--dry-run", **transport)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count("--env e2b"), 2)
                self.assertNotIn("--env e2b_prebuilt", result.stdout)
                self.assertNotIn("--env qz", result.stdout)
                self.assertIn("-a oracle", result.stdout)

    def test_execution_requires_a_key_and_rejects_sdk_debug_mode(self):
        result = self.run_launcher("--execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("E2B_API_KEY", result.stderr)
        for debug in ("true", "TRUE", "tRuE"):
            result = self.run_launcher("--execute", E2B_API_KEY="e2b_fake", E2B_DEBUG=debug)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("E2B_DEBUG", result.stderr)
            self.assertNotIn("e2b_fake", result.stdout + result.stderr)

    def test_refuses_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_launcher("--dry-run", OUTPUT_PATH=directory)
            self.assertNotEqual(result.returncode, 0)

    def test_execute_checks_results_and_cleanup_after_each_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "e2b"
            sdk.mkdir()
            (sdk / "__init__.py").write_text(
                "from dataclasses import dataclass\n"
                "@dataclass\n"
                "class SandboxQuery:\n"
                "    metadata: dict\n"
                "    state: object = None\n"
                "class Pager:\n"
                "    has_next = True\n"
                "    async def next_items(self):\n"
                "        self.has_next = False\n"
                "        return []\n"
                "class AsyncSandbox:\n"
                "    @staticmethod\n"
                "    def list(query, **kwargs):\n"
                "        assert query.metadata == {'session_id': 'trial__env'}\n"
                "        return Pager()\n"
            )
            runner = root / "harbor"
            runner.write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                'while (( $# )); do\n'
                '  if [[ "$1" == -o ]]; then out="$2"; shift; fi\n'
                '  shift\n'
                'done\n'
                'mkdir -p "$out/job/trial/artifacts"\n'
                'printf "NATIVE-E2B\\n" > "$out/job/trial/artifacts/e2b-smoke.txt"\n'
                'cat > "$out/job/trial/result.json" <<\'JSON\'\n'
                '{"trial_name":"trial","finished_at":"2026-01-01",'
                '"config":{"environment":{"type":"e2b"}},'
                '"verifier_result":{"rewards":{"reward":1}}}\nJSON\n'
            )
            runner.chmod(0o755)
            result = self.run_launcher(
                "--execute", E2B_API_KEY="e2b_fake", HARBOR_CLI_BIN=str(runner),
                HARBOR_OPIK_PYTHON=sys.executable, PYTHONPATH=str(root),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("artifact verified"), 2)
            self.assertEqual(result.stdout.count("no running or paused sandboxes"), 2)


class E2BSmokeTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("e2b_smoke", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = self.root / "job" / "trial"
        (self.trial / "artifacts").mkdir(parents=True)
        self.result = {
            "trial_name": "e2b-native__example",
            "finished_at": "2026-01-01T00:00:00Z",
            "exception_info": None,
            "config": {"environment": {"type": "e2b", "import_path": None}},
            "verifier_result": {"rewards": {"reward": 1}},
        }
        self.write_result()
        (self.trial / "artifacts" / "e2b-smoke.txt").write_text("NATIVE-E2B\n")

    def write_result(self):
        (self.trial / "result.json").write_text(json.dumps(self.result))

    def test_accepts_finished_native_trial_and_downloaded_artifact(self):
        (self.root / "job" / "result.json").write_text('{"stats": {}}')
        self.assertEqual(
            self.module.check_results(self.root), ["e2b-native__example__env"]
        )

    def test_rejects_missing_and_duplicate_trials(self):
        with self.assertRaises(ValueError):
            self.module.check_results(self.root / "missing")
        (self.root / "extra").mkdir()
        (self.root / "extra" / "result.json").write_text(json.dumps(self.result))
        with self.assertRaises(ValueError):
            self.module.check_results(self.root)

    def test_rejects_failure_unfinished_and_non_native_results(self):
        for change in (
            {"exception_info": {"exception_type": "EnvironmentStartError"}},
            {"finished_at": None},
            {"verifier_result": {"rewards": {"reward": 0}}},
            {"config": {"environment": {"type": "docker"}}},
            {"config": {"environment": {"type": "e2b", "import_path": "e2b_prebuilt:PrebuiltE2BEnvironment"}}},
        ):
            with self.subTest(change=change):
                original = self.result.copy()
                self.result.update(change)
                self.write_result()
                with self.assertRaises(ValueError):
                    self.module.check_results(self.root)
                self.result = original

    def test_requires_downloaded_artifact_contents(self):
        artifact = self.trial / "artifacts" / "e2b-smoke.txt"
        artifact.write_text("wrong\n")
        with self.assertRaises(ValueError):
            self.module.check_results(self.root)
        artifact.unlink()
        with self.assertRaises(ValueError):
            self.module.check_results(self.root)

    def test_cleanup_checks_all_pages_and_only_trial_sessions(self):
        queries = []

        class Pager:
            def __init__(self):
                self.pages = [[], [SimpleNamespace(sandbox_id="leftover")]]

            @property
            def has_next(self):
                return bool(self.pages)

            async def next_items(self):
                return self.pages.pop(0)

        def list_sandboxes(**kwargs):
            queries.append(kwargs)
            return Pager()

        with self.assertRaises(ValueError):
            asyncio.run(self.module.check_cleanup(["trial__env"], list_sandboxes))
        self.assertEqual(queries[0]["query"].metadata, {"session_id": "trial__env"})
        self.assertIsNone(queries[0]["query"].state)

    def test_cleanup_absence_and_api_failure(self):
        class EmptyPager:
            has_next = True

            async def next_items(self):
                self.has_next = False
                return []

        asyncio.run(self.module.check_cleanup(["trial__env"], lambda **kw: EmptyPager()))

        def failing_list(**kwargs):
            raise RuntimeError("unavailable")

        with self.assertRaises(RuntimeError):
            asyncio.run(self.module.check_cleanup(["trial__env"], failing_list))

    def test_cli_does_not_print_provider_error_contents(self):
        import io

        output = io.StringIO()
        with patch.object(self.module, "check_results", side_effect=ValueError("secret")), patch(
            "sys.stderr", output
        ):
            self.assertEqual(self.module.main([str(self.root)]), 1)
        self.assertNotIn("secret", output.getvalue())


class E2BSmokeTaskTest(unittest.TestCase):
    def test_solution_verifier_and_fresh_sandbox_check(self):
        task = SCRIPT.parents[4] / "Tasks/Sandbox-smoke/e2b-native"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            app.mkdir()
            (app / "input.txt").write_bytes((task / "environment/input.txt").read_bytes())

            def run(relative):
                script = (task / relative).read_text().replace("/app/", f"{app}/")
                script = script.replace("/logs/", f"{root}/logs/")
                return subprocess.run(["bash", "-c", script], capture_output=True, check=False)

            self.assertNotEqual(run("tests/test.sh").returncode, 0)
            self.assertEqual((root / "logs/verifier/reward.txt").read_text(), "0\n")
            self.assertEqual(run("solution/solve.sh").returncode, 0)
            self.assertEqual(run("tests/test.sh").returncode, 0)
            self.assertEqual((root / "logs/verifier/reward.txt").read_text(), "1\n")
            self.assertEqual((root / "logs/artifacts/e2b-smoke.txt").read_text(), "NATIVE-E2B\n")
            self.assertNotEqual(run("solution/solve.sh").returncode, 0)
            (app / "answer.txt").write_text("incorrect\n")
            self.assertNotEqual(run("tests/test.sh").returncode, 0)
            self.assertEqual((root / "logs/verifier/reward.txt").read_text(), "0\n")


if __name__ == "__main__":
    unittest.main()
