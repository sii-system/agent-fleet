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
