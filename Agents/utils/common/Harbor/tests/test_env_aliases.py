from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]


class HarborEnvAliasTests(unittest.TestCase):
    def load_env(self, **overrides: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        'source "$1"; source "$1"; python3 -c '
                        "'import json, os; print(json.dumps(dict(os.environ)))'"
                    ),
                    "bash",
                    str(HARBOR_DIR / "env.sh"),
                ],
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": tmp,
                    "AGENT_FLEET_CONFIG_LOADED_ROOT": str(HARBOR_DIR.parents[3]),
                    "AGENT_FLEET_PATHS_FILE": f"{tmp}/missing.env",
                    "AGENT_FLEET_RUNTIME_DIR": f"{tmp}/runtime",
                    "OPIK_URL": "",
                    **overrides,
                },
                capture_output=True,
                text=True,
                check=True,
            )
        return json.loads(result.stdout)

    def test_defaults_export_only_canonical_run_settings(self) -> None:
        env = self.load_env()
        self.assertEqual(env["HARBOR_N_ATTEMPTS"], "1")
        self.assertEqual(env["HARBOR_MAX_RETRIES"], "2")
        self.assertEqual(env["HARBOR_INCLUDE_TASKS"], "")
        for old in ("N_ATTEMPTS", "HARBOR_RUNS", "MAX_RETRIES", "INCLUDE_TASKS"):
            self.assertNotIn(old, env)

    def test_summary_and_analyzer_have_independent_opt_in_defaults(self) -> None:
        env = self.load_env()
        self.assertEqual(env["HARBOR_SUMMARY_ENABLED"], "1")
        self.assertEqual(env["HARBOR_ANALYZER_ENABLED"], "0")
        env = self.load_env(HARBOR_MONITOR_ENABLED="0", HARBOR_SUMMARY_ENABLED="0")
        self.assertEqual(env["HARBOR_SUMMARY_ENABLED"], "0")
        self.assertEqual(env["HARBOR_ANALYZER_ENABLED"], "0")
        env = self.load_env(HARBOR_ANALYZER_ENABLED="1")
        self.assertEqual(env["HARBOR_ANALYZER_ENABLED"], "1")
        self.assertEqual(env["HARBOR_SUMMARY_ENABLED"], "1")

    def test_summary_uses_shared_gateway_not_agent_proxy_credentials(self) -> None:
        overrides = {"API_KEY": "shared-key", "BASE_URL": "https://gateway.invalid/v1",
                     "ANTHROPIC_AUTH_TOKEN": "agent-placeholder",
                     "ANTHROPIC_BASE_URL": "https://agent-proxy.invalid"}
        env = self.load_env(**overrides)
        self.assertEqual(env["HARBOR_ANALYZER_API_KEY"], "shared-key")
        self.assertEqual(env["HARBOR_ANALYZER_BASE_URL"], "https://gateway.invalid/v1")
        env = self.load_env(**overrides, HARBOR_ANALYZER_API_KEY="summary-key",
                            HARBOR_ANALYZER_BASE_URL="https://summary.invalid/v1")
        self.assertEqual(env["HARBOR_ANALYZER_API_KEY"], "summary-key")
        self.assertEqual(env["HARBOR_ANALYZER_BASE_URL"], "https://summary.invalid/v1")

    def test_custom_rollout_declarations_survive_sourcing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout_env = Path(tmp) / "rollout.env"
            rollout_env.write_text(
                "declare -x RL_PORT=19999\ndeclare -x RL_ENVIRONMENT_TYPE=qz\n"
            )
            env = self.load_env(ROLLOUT="1", RL_ENV_FILE=str(rollout_env))
        self.assertEqual(env["RL_PORT"], "19999")
        self.assertEqual(env["RL_ENVIRONMENT_TYPE"], "qz")
        self.assertEqual(env["HARBOR_ENVIRONMENT_TYPE"], "qz")

    def test_web_mcp_rejects_managed_backends_with_cold_or_warm_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for cached in (False, True):
                if cached:
                    subprocess.run(
                        ["python3", str(HARBOR_DIR.parent / "mcp/build.py"), tmp],
                        check=True,
                        capture_output=True,
                    )
                for backend in ("e2b", "qz"):
                    for setting in ("HARBOR_ENVIRONMENT_TYPE", "RL_ENVIRONMENT_TYPE"):
                        with self.subTest(cached=cached, backend=backend, setting=setting):
                            with self.assertRaises(subprocess.CalledProcessError) as error:
                                self.load_env(
                                    HARBOR_CC_WEB_MCP_ENABLED="1",
                                    LOCAL_WHEEL_DIR=tmp,
                                    **{setting: backend},
                                )
                            self.assertIn(
                                f"Web MCP is not supported on {backend}",
                                error.exception.stderr,
                            )

    def test_web_mcp_backend_guard_preserves_supported_and_disabled_modes(self) -> None:
        for backend in ("docker", "opensandbox", "e2b", "qz"):
            modes = ("0", "1") if backend in ("docker", "opensandbox") else ("0",)
            for enabled in modes:
                with self.subTest(backend=backend, enabled=enabled):
                    env = self.load_env(
                        HARBOR_ENVIRONMENT_TYPE=backend,
                        HARBOR_CC_WEB_MCP_ENABLED=enabled,
                        HARBOR_CC_WEB_MCP_SOURCE="/ignored/stale-mcp.pyz",
                    )
                    self.assertEqual(bool(env["HARBOR_CC_WEB_MCP_SOURCE"]), enabled == "1")

    def test_web_mcp_guard_checks_backend_from_custom_rollout_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout_env = Path(tmp) / "rollout.env"
            rollout_env.write_text("declare -x RL_ENVIRONMENT_TYPE=qz\n")
            with self.assertRaises(subprocess.CalledProcessError) as error:
                self.load_env(
                    ROLLOUT="1", RL_ENV_FILE=str(rollout_env), HARBOR_CC_WEB_MCP_ENABLED="1"
                )
            self.assertIn("Web MCP is not supported on qz", error.exception.stderr)

    def test_legacy_inputs_resolve_to_canonical_settings(self) -> None:
        for old, new, value in (
            ("N_ATTEMPTS", "HARBOR_N_ATTEMPTS", "3"),
            ("HARBOR_RUNS", "HARBOR_N_ATTEMPTS", "4"),
            ("MAX_RETRIES", "HARBOR_MAX_RETRIES", "0"),
            ("INCLUDE_TASKS", "HARBOR_INCLUDE_TASKS", "task-a,task-b"),
        ):
            with self.subTest(alias=old):
                self.assertEqual(self.load_env(**{old: value})[new], value)

    def test_canonical_inputs_win_over_conflicting_aliases(self) -> None:
        env = self.load_env(
            HARBOR_N_ATTEMPTS="2",
            N_ATTEMPTS="3",
            HARBOR_RUNS="4",
            HARBOR_MAX_RETRIES="0",
            MAX_RETRIES="5",
            HARBOR_INCLUDE_TASKS="task-a",
            INCLUDE_TASKS="task-b",
        )
        self.assertEqual(env["HARBOR_N_ATTEMPTS"], "2")
        self.assertEqual(env["HARBOR_MAX_RETRIES"], "0")
        self.assertEqual(env["HARBOR_INCLUDE_TASKS"], "task-a")

    def test_explicit_empty_canonical_filter_blocks_legacy_filter(self) -> None:
        env = self.load_env(HARBOR_INCLUDE_TASKS="", INCLUDE_TASKS="stale-task")
        self.assertEqual(env["HARBOR_INCLUDE_TASKS"], "")

    def test_empty_numeric_inputs_use_aliases_or_defaults(self) -> None:
        for aliases, expected in (
            ({}, ("1", "2")),
            ({"N_ATTEMPTS": "3", "MAX_RETRIES": "0"}, ("3", "0")),
            ({"N_ATTEMPTS": "3", "HARBOR_RUNS": "4"}, ("4", "2")),
        ):
            with self.subTest(aliases=aliases):
                env = self.load_env(
                    HARBOR_N_ATTEMPTS="", HARBOR_MAX_RETRIES="", **aliases
                )
                self.assertEqual(
                    (env["HARBOR_N_ATTEMPTS"], env["HARBOR_MAX_RETRIES"]), expected
                )

    def test_harbor_runs_wins_when_both_legacy_attempt_names_are_set(self) -> None:
        env = self.load_env(N_ATTEMPTS="3", HARBOR_RUNS="4")
        self.assertEqual(env["HARBOR_N_ATTEMPTS"], "4")

    def test_opik_endpoints_follow_url_despite_stale_derived_values(self) -> None:
        for url, base in (
            ("https://opik.example/api", "https://opik.example"),
            ("https://opik.example/api/", "https://opik.example"),
            ("https://opik.example", "https://opik.example"),
            ("", ""),
        ):
            with self.subTest(url=url):
                env = self.load_env(
                    OPIK_URL=url,
                    OPIK_URL_OVERRIDE="https://stale.example/api",
                    OPIK_BASE="https://stale.example",
                )
                self.assertEqual(env["OPIK_URL_OVERRIDE"], url)
                self.assertEqual(env["OPIK_BASE"], base)

    def test_queue_worker_preserves_attempts_and_retries_in_child(self) -> None:
        source = (HARBOR_DIR / "run_harbor_worker.sh").read_text()
        worker = re.search(
            r"^run_claimed_task\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(worker)
        for overrides in (
            {"HARBOR_N_ATTEMPTS": "2", "HARBOR_MAX_RETRIES": "0"},
            {"HARBOR_RUNS": "3", "MAX_RETRIES": "1"},
            {"N_ATTEMPTS": "4", "MAX_RETRIES": "0"},
        ):
            with (
                self.subTest(overrides=overrides),
                tempfile.TemporaryDirectory() as tmp,
            ):
                env = self.load_env(AGENT="opencode", **overrides)
                expected = (env["HARBOR_N_ATTEMPTS"], env["HARBOR_MAX_RETRIES"])
                (Path(tmp) / "harboropik.sh").write_text(
                    '#!/usr/bin/env bash\nset -euo pipefail\nsource "$HARBOR_TEST_ENV"\n'
                    "python3 -c 'import json, os; print(json.dumps(dict(os.environ)))'\n"
                )
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        worker[0] + "\n"
                        "harbor_agent_is_opencode() { return 0; }\n"
                        "seta_online_early_stop_enabled() { return 1; }\n"
                        'run_claimed_task task-a "$HOME/jobs" 1',
                    ],
                    env={
                        **env,
                        "SCRIPT_DIR": tmp,
                        "HOME": tmp,
                        "HARBOR_TEST_ENV": str(HARBOR_DIR / "env.sh"),
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                child = json.loads(result.stdout)
                self.assertEqual(
                    (child["HARBOR_N_ATTEMPTS"], child["HARBOR_MAX_RETRIES"]), expected
                )
                self.assertEqual(child["HARBOR_INCLUDE_TASKS"], "task-a")
                self.assertEqual(child["HARBOR_N_CONCURRENT"], "1")

    def test_opencode_runs_each_attempt_once_for_local_and_registry_datasets(
        self,
    ) -> None:
        for dataset in ("auto", "terminalbench21"):
            with self.subTest(dataset=dataset), tempfile.TemporaryDirectory() as tmp:
                for relative in (
                    "dataset",
                    "run/runtime/opencode",
                    "run/queue/opencode",
                ):
                    (Path(tmp) / relative).mkdir(parents=True)
                env = self.load_env(
                    AGENT="opencode",
                    DATASET_NAME=dataset,
                    DATASET_PATH=f"{tmp}/dataset",
                    OUTPUT_PATH=f"{tmp}/run",
                    HARBOR_ENVIRONMENT_TYPE="e2b",
                    HARBOR_DRY_RUN="1",
                    HARBOR_N_ATTEMPTS="3",
                    HARBOR_MAX_RETRIES="0",
                    HARBOR_INCLUDE_TASKS="fix-git",
                )
                result = subprocess.run(
                    ["bash", str(HARBOR_DIR / "harboropik.sh")],
                    env={**env, "HOME": tmp},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.count("[INFO] attempt "), 3)
                self.assertEqual(result.stdout.count("  -k\n  1\n"), 3)
                self.assertIn("attempt 3/3 trial_id=attempt-3", result.stdout)
                task = (
                    "terminal-bench/fix-git"
                    if dataset == "terminalbench21"
                    else "fix-git"
                )
                self.assertEqual(result.stdout.count(f"  -i\n  {task}\n"), 3)


if __name__ == "__main__":
    unittest.main()
