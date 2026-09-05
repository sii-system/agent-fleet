import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "run_fleet.sh"


class FleetRouterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"

        harbor = self.repo / "Agents/utils/common/Harbor/start.sh"
        harbor.parent.mkdir(parents=True)
        harbor.write_text(
            """#!/usr/bin/env bash
printf 'runner=harbor\\n'
printf 'DATASET_NAME=%s\\n' "${DATASET_NAME-}"
printf 'DATASET_PATH=%s\\n' "${DATASET_PATH-}"
printf 'AGENT=%s\\n' "${AGENT-}"
printf 'TOTAL_WORKERS=%s\\n' "${TOTAL_WORKERS-}"
printf 'HARBOR_N_CONCURRENT=%s\\n' "${HARBOR_N_CONCURRENT-}"
printf 'FLEET_TASKS=%s\\n' "${FLEET_TASKS-}"
printf 'RUN_ID=%s\\n' "${RUN_ID-}"
printf 'OUTPUT_PATH=%s\\n' "${OUTPUT_PATH-}"
printf 'BASE_URL=%s\\n' "${BASE_URL-}"
printf 'API_KEY=%s\\n' "${API_KEY-}"
printf 'MODEL=%s\\n' "${MODEL-}"
printf 'RL_ENVIRONMENT_TYPE=%s\\n' "${RL_ENVIRONMENT_TYPE-}"
printf 'QZ_SANDBOX_TEMPLATE=%s\\n' "${QZ_SANDBOX_TEMPLATE-}"
if [[ -n "${SBX_API_KEY-}" ]]; then
  printf 'SBX_API_KEY_SET=1\\n'
else
  printf 'SBX_API_KEY_SET=0\\n'
fi
exit "${STUB_EXIT:-0}"
""",
            encoding="utf-8",
        )

        pinchbench = self.repo / "Tasks/Pinchbench/scripts/run-parallel-workers.py"
        pinchbench.parent.mkdir(parents=True)
        pinchbench.write_text(
            """import os
import sys
print("runner=pinchbench")
print("args=" + " ".join(sys.argv[1:]))
print("PINCHBENCH_EXACT_TASK_SELECTION=" + os.environ.get("PINCHBENCH_EXACT_TASK_SELECTION", ""))
print("RUN_ID=" + os.environ.get("RUN_ID", ""))
raise SystemExit(int(os.environ.get("STUB_EXIT", "0")))
""",
            encoding="utf-8",
        )

        clawbio = self.repo / "Tasks/clawBio/scripts/run-openclaw-clawbio.sh"
        clawbio.parent.mkdir(parents=True)
        clawbio.write_text(
            """#!/usr/bin/env bash
printf 'runner=clawbio\\n'
printf 'COUNT=%s\\n' "${COUNT-}"
printf 'args=%s\\n' "$*"
printf 'RUN_ID=%s\\n' "${RUN_ID-}"
exit "${STUB_EXIT:-0}"
""",
            encoding="utf-8",
        )

        browsecomp = self.repo / "Tasks/BrowseComp-Plus/scripts/run.sh"
        browsecomp.parent.mkdir(parents=True)
        browsecomp.write_text(
            """#!/usr/bin/env bash
printf 'runner=browsecomp-plus\\n'
printf 'args=%s\\n' "$*"
printf 'RUN_ID=%s\\n' "${RUN_ID-}"
printf 'OUTPUT_PATH=%s\\n' "${OUTPUT_PATH-}"
exit "${STUB_EXIT:-0}"
""",
            encoding="utf-8",
        )
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://gateway.example.invalid\n"
            "API_KEY=fake-runner-key\n"
            "MODEL=test-model\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_fleet(self, *args, extra_env=None, input_text=None):
        env = os.environ.copy()
        for name in (
            "AGENT",
            "TOTAL_WORKERS",
            "HARBOR_N_CONCURRENT",
            "DATASET_NAME",
            "DATASET_PATH",
            "FLEET_TASKS",
            "RUN_ID",
            "BASE_URL",
            "API_KEY",
            "AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "MODEL",
            "HARBOR_MODEL",
            "TRACE_TO_OPIK",
            "OPIK_URL",
            "RL_ENVIRONMENT_TYPE",
            "HARBOR_ENVIRONMENT_TYPE",
            "QZ_SANDBOX_TEMPLATE",
            "SBX_API_KEY",
        ):
            env.pop(name, None)
        env["REPO_DIR"] = str(self.repo)
        env.update(extra_env or {})
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=self.root,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_harbor_registry_handoff(self):
        result = self.run_fleet(
            "--taskset",
            "terminal-bench/terminal-bench-2-1",
            "--agent",
            "claude-code",
            "--workers",
            "3",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=harbor", result.stdout)
        self.assertIn("DATASET_NAME=terminal-bench/terminal-bench-2-1", result.stdout)
        self.assertIn("AGENT=claude-code", result.stdout)
        self.assertIn("TOTAL_WORKERS=3", result.stdout)
        self.assertIn("HARBOR_N_CONCURRENT=3", result.stdout)
        self.assertRegex(result.stdout, r"RUN_ID=fleet-direct-[0-9]{8}-[0-9]{6}-[0-9]+\n")

    def test_explicit_local_taskset_maps_only_path_inputs(self):
        result = self.run_fleet("--taskset", "./tasks", "--agent", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=harbor", result.stdout)
        self.assertIn("DATASET_NAME=auto", result.stdout)
        self.assertIn(f"DATASET_PATH={self.root}/./tasks", result.stdout)

    def test_qz_opencode_config_reaches_harbor_entrypoint(self):
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://gateway.example.invalid\n"
            "API_KEY=fake-runner-key\n"
            "MODEL=test-model\n"
            "RL_ENVIRONMENT_TYPE=qz\n"
            "SBX_API_KEY=sbx_fake_qz_key\n"
            "QZ_SANDBOX_TEMPLATE=current_template\n",
            encoding="utf-8",
        )

        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            "--agent",
            "opencode",
            "--workers",
            "1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AGENT=opencode", result.stdout)
        self.assertIn("RL_ENVIRONMENT_TYPE=qz", result.stdout)
        self.assertIn("QZ_SANDBOX_TEMPLATE=current_template", result.stdout)
        self.assertIn("SBX_API_KEY_SET=1", result.stdout)
        self.assertNotIn("sbx_fake_qz_key", result.stdout)

    def test_pinchbench_routes_to_openclaw_runner(self):
        result = self.run_fleet(
            "--taskset", "pinchbench", "--agent", "openclaw", "--workers", "4"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=pinchbench", result.stdout)
        self.assertIn("args=--instances 4", result.stdout)
        self.assertIn("PINCHBENCH_EXACT_TASK_SELECTION=0", result.stdout)
        self.assertRegex(result.stdout, r"RUN_ID=fleet-direct-[0-9]{8}-[0-9]{6}-[0-9]+\n")

    def test_browsecomp_routes_to_benchmark_runner_with_harness_concurrency(self):
        result = self.run_fleet(
            "--taskset", "browsecomp-plus", "--task", "q1,q2", "--agent", "pi", "--workers", "2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=browsecomp-plus", result.stdout)
        self.assertIn("--task q1,q2", result.stdout)
        self.assertIn("--agent pi", result.stdout)
        self.assertIn("--workers 2", result.stdout)

    def test_direct_run_ignores_inherited_run_identity(self):
        results = [
            self.run_fleet(
                "--taskset",
                "browsecomp-plus",
                "--agent",
                "pi",
                extra_env={
                    "RUN_ID": "stale-run",
                    "OUTPUT_PATH": "/tmp/stale-run",
                },
            )
            for _ in range(2)
        ]

        run_ids = []
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            match = re.search(
                r"^RUN_ID=(fleet-direct-[0-9]{8}-[0-9]{6}-[0-9]+)$",
                result.stdout,
                re.MULTILINE,
            )
            self.assertIsNotNone(match)
            run_ids.append(match.group(1))
            self.assertIn("OUTPUT_PATH=\n", result.stdout)
        self.assertNotEqual(run_ids[0], run_ids[1])

    def test_explicit_run_id_is_forwarded_without_inherited_output_path(self):
        result = self.run_fleet(
            "--taskset",
            "browsecomp-plus",
            "--agent",
            "pi",
            "--run-id",
            "fresh-run",
            extra_env={"OUTPUT_PATH": "/tmp/stale-run"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RUN_ID=fresh-run\n", result.stdout)
        self.assertIn("OUTPUT_PATH=\n", result.stdout)

    def test_browsecomp_task_preflight_reaches_validation_mode(self):
        result = self.run_fleet(
            "--taskset", "browsecomp", "--task", "q1", "--validate-task-selection"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--validate-tasks-only", result.stdout)

    def test_task_selection_is_normalized_once_and_routed(self):
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            "--task",
            " fix-git, break-filter-js-from-html ,,fix-git ",
            "--task",
            "build-cython-ext",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "FLEET_TASKS=fix-git,break-filter-js-from-html,build-cython-ext",
            result.stdout,
        )

    def test_openclaw_task_selection_uses_exact_runner_modes(self):
        pinchbench = self.run_fleet(
            "--taskset", "pinchbench", "--task", "task_sanity,task_weather"
        )
        self.assertEqual(pinchbench.returncode, 0, pinchbench.stderr)
        self.assertIn("--suite task_sanity,task_weather", pinchbench.stdout)
        self.assertIn("PINCHBENCH_EXACT_TASK_SELECTION=1", pinchbench.stdout)

        clawbio = self.run_fleet(
            "--taskset", "clawbio", "--task", "rnaseq-de-demo,fine-mapping-demo"
        )
        self.assertEqual(clawbio.returncode, 0, clawbio.stderr)
        self.assertIn(
            "args=--tasks rnaseq-de-demo,fine-mapping-demo",
            clawbio.stdout,
        )

    def test_openclaw_task_preflight_uses_validation_only_mode(self):
        for taskset in ("pinchbench", "clawbio"):
            with self.subTest(taskset=taskset):
                result = self.run_fleet(
                    "--taskset",
                    taskset,
                    "--task",
                    "selected-task",
                    "--validate-task-selection",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--validate-tasks-only", result.stdout)

    def test_harbor_clears_inherited_task_selection_when_task_is_omitted(self):
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            extra_env={"FLEET_TASKS": "stale-selection"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FLEET_TASKS=\n", result.stdout)
        self.assertNotIn("FLEET_TASKS=stale-selection", result.stdout)

    def test_clawbio_routes_to_openclaw_launcher(self):
        result = self.run_fleet(
            "--taskset", "clawbio", "--agent", "openclaw", "--workers", "5"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=clawbio", result.stdout)
        self.assertIn("COUNT=5", result.stdout)
        self.assertRegex(result.stdout, r"RUN_ID=fleet-direct-[0-9]{8}-[0-9]{6}-[0-9]+\n")

    def test_openclaw_tasksets_route_without_agent(self):
        pinchbench = self.run_fleet("--taskset", "pinchbench")
        self.assertEqual(pinchbench.returncode, 0, pinchbench.stderr)
        self.assertIn("runner=pinchbench", pinchbench.stdout)

        clawbio = self.run_fleet("--taskset", "clawbio")
        self.assertEqual(clawbio.returncode, 0, clawbio.stderr)
        self.assertIn("runner=clawbio", clawbio.stdout)

    def test_openclaw_agent_mismatch_reports_requested_and_actual_agents(self):
        result = self.run_fleet(
            "--taskset", "clawbio", "--agent", "opencode", "--workers", "1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=clawbio", result.stdout)
        self.assertIn("requested agent: opencode", result.stderr)
        self.assertIn("taskset: clawbio", result.stderr)
        self.assertIn("actual agent: openclaw", result.stderr)

    def test_caller_agent_environment_is_preserved(self):
        result = self.run_fleet(
            "--taskset", "terminalbench21", extra_env={"AGENT": "opencode"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=harbor", result.stdout)
        self.assertIn("AGENT=opencode", result.stdout)

    def test_downstream_exit_code_is_returned_unchanged(self):
        result = self.run_fleet(
            "--taskset", "terminalbench21", extra_env={"STUB_EXIT": "17"}
        )
        self.assertEqual(result.returncode, 17)

    def test_missing_config_fails_before_starting_runner(self):
        (self.repo / "config.local.env").unlink()

        result = self.run_fleet("--taskset", "terminalbench21")

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing required configuration", result.stderr)
        self.assertIn("./scripts/setup.sh", result.stderr)
        self.assertIn("config.local.env", result.stderr)
        self.assertNotIn("runner=harbor", result.stdout)

    def test_task_structure_and_support_fail_before_config_preflight(self):
        (self.repo / "config.local.env").unlink()

        missing_taskset = self.run_fleet("--task", "task_sanity")
        self.assertEqual(missing_taskset.returncode, 2)
        self.assertIn("--task requires --taskset", missing_taskset.stderr)
        self.assertNotIn("missing required configuration", missing_taskset.stderr)

        unsupported = self.run_fleet(
            "--taskset", "publisher/private-registry", "--task", "task_sanity"
        )
        self.assertEqual(unsupported.returncode, 2)
        self.assertIn("--task is unsupported", unsupported.stderr)
        self.assertNotIn("missing required configuration", unsupported.stderr)

    def test_task_rejects_empty_and_control_character_values(self):
        for task in (" , , ", "task-one\n task-two"):
            with self.subTest(task=task):
                result = self.run_fleet(
                    "--taskset", "terminalbench21", "--task", task
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("--task must contain", result.stderr)
                self.assertNotIn("runner=", result.stdout)

    def test_task_rejects_missing_value_before_following_option(self):
        result = self.run_fleet(
            "--taskset", "terminalbench21", "--task", "--dry-run"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--task requires a task name", result.stderr)
        self.assertNotIn("runner=", result.stdout)

    def test_task_rejects_rollout_mode(self):
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            "--task",
            "fix-git",
            extra_env={"ROLLOUT": "1"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported when ROLLOUT=1", result.stderr)
        self.assertNotIn("runner=", result.stdout)

    def test_empty_opik_url_does_not_block_startup(self):
        # OPIK_URL is the single switch for tracing: an empty/unset value
        # means run without Opik, not a missing-configuration error.
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://gateway.example.invalid\n"
            "API_KEY=fake-runner-key\n"
            "MODEL=test-model\n",
            encoding="utf-8",
        )

        result = self.run_fleet("--taskset", "terminalbench21")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("missing required configuration", result.stderr)
        self.assertIn("runner=harbor", result.stdout)

    def test_tool_aliases_do_not_override_saved_global_config(self):
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            extra_env={
                "AUTH_TOKEN": "fake-caller-token",
                "ANTHROPIC_BASE_URL": "https://alias-gateway.example.invalid",
                "ANTHROPIC_AUTH_TOKEN": "fake-alias-token",
                "HARBOR_MODEL": "alias-model",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASE_URL=https://gateway.example.invalid", result.stdout)
        self.assertIn("API_KEY=fake-runner-key", result.stdout)
        self.assertIn("MODEL=test-model", result.stdout)

    def test_auth_token_supplies_a_missing_api_key(self):
        (self.repo / "config.local.env").write_text(
            "BASE_URL=https://gateway.example.invalid\n"
            "MODEL=test-model\n",
            encoding="utf-8",
        )

        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            extra_env={"AUTH_TOKEN": "fake-caller-token"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("API_KEY=fake-caller-token", result.stdout)

    def test_runtime_canonical_config_overrides_saved_config(self):
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            extra_env={
                "BASE_URL": "https://runtime-gateway.example.invalid",
                "API_KEY": "fake-runtime-token",
                "MODEL": "runtime-model",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "BASE_URL=https://runtime-gateway.example.invalid",
            result.stdout,
        )
        self.assertIn("API_KEY=fake-runtime-token", result.stdout)
        self.assertIn("MODEL=runtime-model", result.stdout)

    def test_saved_local_config_wins_over_public_config(self):
        (self.repo / "config.env").write_text(
            "BASE_URL=https://public-gateway.example.invalid\n"
            "API_KEY=fake-public-token\n"
            "MODEL=public-model\n",
            encoding="utf-8",
        )

        result = self.run_fleet("--taskset", "terminalbench21")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASE_URL=https://gateway.example.invalid", result.stdout)
        self.assertIn("API_KEY=fake-runner-key", result.stdout)
        self.assertIn("MODEL=test-model", result.stdout)

    def test_direct_output_writes_replayable_spec_before_runner(self):
        output = self.root / "fleet-spec.json"
        result = self.run_fleet(
            "--taskset",
            "terminal-bench/terminal-bench-2-1",
            "--agent",
            "claude-code",
            "--workers",
            "3.0",
            "--output",
            str(output),
            extra_env={"STUB_EXIT": "17"},
        )

        self.assertEqual(result.returncode, 17)
        self.assertIn("FleetSpec written", result.stderr)
        self.assertIn("TOTAL_WORKERS=3", result.stdout)
        self.assertNotIn("TOTAL_WORKERS=3.0", result.stdout)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "taskset": "terminal-bench/terminal-bench-2-1",
                "agent": "claude-code",
                "workers": 3,
            },
        )
        self.assertEqual(list(self.root.glob(".fleet-spec.*")), [])

        replay = self.run_fleet("--spec", str(output), "--dry-run")
        direct = self.run_fleet(
            "--taskset",
            "terminal-bench/terminal-bench-2-1",
            "--agent",
            "claude-code",
            "--workers",
            "3",
            "--dry-run",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(replay.stdout, direct.stdout)

    def test_direct_output_normalizes_and_replays_task_selection(self):
        output = self.root / "fleet-spec.json"
        result = self.run_fleet(
            "--taskset",
            "terminalbench21",
            "--task",
            " fix-git,break-filter-js-from-html ",
            "--task",
            "fix-git",
            "--output",
            str(output),
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "taskset": "terminalbench21",
                "task": "fix-git,break-filter-js-from-html",
            },
        )
        replay = self.run_fleet("--spec", str(output), "--dry-run")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(replay.stdout, result.stdout)

    def test_dash_prefixed_task_id_uses_equals_form_and_replays(self):
        output = self.root / "fleet-spec.json"
        direct = self.run_fleet(
            "--taskset",
            "./tasks",
            "--task=-smoke",
            "--output",
            str(output),
            "--dry-run",
        )

        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["task"],
            "-smoke",
        )
        replay = self.run_fleet("--spec", str(output), "--dry-run")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(replay.stdout, direct.stdout)

    def test_direct_output_saves_only_explicit_fields(self):
        output = self.root / "fleet-spec.json"
        result = self.run_fleet(
            "-t",
            "terminalbench21",
            "-o",
            str(output),
            "--dry-run",
            extra_env={"AGENT": "opencode", "TOTAL_WORKERS": "8"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {"schema_version": 1, "taskset": "terminalbench21"},
        )

    def test_direct_output_rejects_option_token_before_runner(self):
        result = self.run_fleet(
            "--taskset", "pinchbench", "--output", "--dry-run"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a file path", result.stderr)
        self.assertNotIn("runner=", result.stdout)
        self.assertFalse((self.root / "--dry-run").exists())

    def test_direct_output_rejects_mistyped_option_token_before_runner(self):
        result = self.run_fleet(
            "--taskset", "pinchbench", "--output", "--dryrn"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a file path", result.stderr)
        self.assertNotIn("runner=", result.stdout)
        self.assertFalse((self.root / "--dryrn").exists())

    def test_direct_output_accepts_dash_prefixed_path_with_dot_slash(self):
        result = self.run_fleet(
            "--taskset", "pinchbench", "--output", "./--dryrn", "--dry-run"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "--dryrn").exists())

    def test_direct_output_rejects_empty_path(self):
        result = self.run_fleet("--taskset", "pinchbench", "--output", "")

        self.assertEqual(result.returncode, 2)
        self.assertIn("non-empty file path", result.stderr)
        self.assertNotIn("runner=", result.stdout)

    def test_direct_output_requires_existing_directory_before_runner(self):
        output = self.root / "missing" / "fleet-spec.json"
        result = self.run_fleet(
            "--taskset", "pinchbench", "--output", str(output)
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("output directory does not exist", result.stderr)
        self.assertNotIn("runner=", result.stdout)
        self.assertFalse(output.exists())

    def test_harbor_dry_run_prints_command_without_starting_runner(self):
        result = self.run_fleet(
            "--taskset",
            "terminal-bench/terminal-bench-2-1",
            "--agent",
            "claude-code",
            "--workers",
            "3",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Command: env", result.stdout)
        self.assertIn("DATASET_NAME=terminal-bench/terminal-bench-2-1", result.stdout)
        self.assertIn("AGENT=claude-code", result.stdout)
        self.assertIn("TOTAL_WORKERS=3", result.stdout)
        self.assertIn("Harbor/start.sh", result.stdout)
        self.assertNotIn("runner=harbor", result.stdout)

    def test_harbor_detach_is_forwarded_to_start(self):
        result = self.run_fleet(
            "--taskset", "terminalbench21", "--detach", "--dry-run"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Harbor/start.sh --detach", result.stdout)

    def test_openclaw_detach_is_reported_and_ignored(self):
        for taskset in ("pinchbench", "clawbio"):
            with self.subTest(taskset=taskset):
                result = self.run_fleet(
                    "--taskset", taskset, "--detach", "--dry-run"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"--detach ignored for taskset: {taskset}", result.stderr)
                self.assertNotIn("--detach", result.stdout)

    def test_spec_file_matches_direct_dry_run(self):
        spec = self.root / "fleet-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "taskset": "terminal-bench/terminal-bench-2-1",
                    "agent": "claude-code",
                    "workers": 3,
                }
            ),
            encoding="utf-8",
        )
        direct = self.run_fleet(
            "--taskset", "terminal-bench/terminal-bench-2-1",
            "--agent", "claude-code", "--workers", "3", "--dry-run",
        )
        from_spec = self.run_fleet("--spec", str(spec), "--dry-run")

        self.assertEqual(from_spec.returncode, 0, from_spec.stderr)
        self.assertEqual(from_spec.stdout, direct.stdout)

    def test_spec_stdin_routes_to_existing_runner(self):
        result = self.run_fleet(
            "--spec",
            "-",
            input_text=json.dumps(
                {"schema_version": 1, "taskset": "pinchbench", "workers": 2}
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner=pinchbench", result.stdout)
        self.assertIn("args=--instances 2", result.stdout)

    def test_spec_stdin_array_routes_multiple_runs_automatically(self):
        result = self.run_fleet(
            "--spec", "-", "--dry-run",
            input_text=json.dumps(
                [
                    {"schema_version": 1, "taskset": "owner/first"},
                    {"schema_version": 1, "taskset": "owner/second"},
                ]
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATASET_NAME=owner/first", result.stdout)
        self.assertIn("DATASET_NAME=owner/second", result.stdout)
        self.assertEqual(result.stdout.count("Command: env"), 2)

    def test_spec_rejects_invalid_documents(self):
        invalid_specs = (
            "not-json",
            (
                '{"schema_version":1,"taskset":"pinchbench"}\n'
                '{"schema_version":1,"taskset":"clawbio"}'
            ),
            {},
            {"schema_version": "1", "taskset": "terminalbench21"},
            {"schema_version": 2, "taskset": "terminalbench21"},
            {"schema_version": 1, "taskset": ""},
            {"schema_version": 1, "taskset": "pinchbench\u0000clawbio"},
            {"schema_version": 1, "taskset": "terminalbench21", "agent": ""},
            {"schema_version": 1, "taskset": "terminalbench21", "workers": 0},
            {"schema_version": 1, "taskset": "terminalbench21", "workers": 1.5},
            {"schema_version": 1, "taskset": "terminalbench21", "workers": 4097},
            {"schema_version": 1, "taskset": "terminalbench21", "workers": 1e20},
            {"schema_version": 1, "taskset": "terminalbench21", "task": 1},
            {"schema_version": 1, "taskset": "terminalbench21", "task": ""},
            {"schema_version": 1, "taskset": "terminalbench21", "task": " , , "},
            {"schema_version": 1, "taskset": "owner/tasks", "task": "task-one"},
            {
                "schema_version": 1,
                "taskset": "terminalbench21",
                "task": "fix-git\nother",
            },
            {"schema_version": 1, "taskset": "terminalbench21", "extra": True},
        )
        for payload in invalid_specs:
            with self.subTest(payload=payload):
                text = payload if isinstance(payload, str) else json.dumps(payload)
                result = self.run_fleet("--spec", "-", input_text=text)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid FleetSpec v1", result.stderr)

    def test_spec_normalizes_integral_float_workers(self):
        # JSON offers no int/float distinction, so 3.0 passes validation as an
        # integral number; it must still reach the runner as "3", never "3.0".
        result = self.run_fleet(
            "--spec", "-", "--dry-run",
            input_text=json.dumps(
                {"schema_version": 1, "taskset": "terminalbench21", "workers": 3.0}
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOTAL_WORKERS=3", result.stdout)
        self.assertNotIn("TOTAL_WORKERS=3.0", result.stdout)

    def test_spec_output_writes_normalized_copy(self):
        output = self.root / "normalized.json"
        result = self.run_fleet(
            "--spec",
            "-",
            "--output",
            str(output),
            "--dry-run",
            input_text=json.dumps(
                {
                    "schema_version": 1,
                    "taskset": "terminalbench21",
                    "task": " fix-git, break-filter-js-from-html,fix-git ",
                    "workers": 3.0,
                }
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "taskset": "terminalbench21",
                "task": "fix-git,break-filter-js-from-html",
                "workers": 3,
            },
        )

    def test_spec_rejects_direct_argument_overrides(self):
        result = self.run_fleet(
            "--spec", "-", "--task", "fix-git",
            input_text=json.dumps(
                {"schema_version": 1, "taskset": "terminalbench21"}
            ),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--spec cannot be combined", result.stderr)

    def test_spec_requires_a_source(self):
        result = self.run_fleet("--spec")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--spec requires", result.stderr)

    def test_openclaw_dry_run_prints_each_runner_without_starting_it(self):
        pinchbench = self.run_fleet(
            "--taskset", "pinchbench", "--agent", "openclaw", "--workers", "4",
            "--dry-run",
        )
        self.assertEqual(pinchbench.returncode, 0, pinchbench.stderr)
        self.assertIn(
            "Command: env PINCHBENCH_EXACT_TASK_SELECTION=0 python3",
            pinchbench.stdout,
        )
        self.assertIn("run-parallel-workers.py --instances 4", pinchbench.stdout)
        self.assertNotIn("runner=pinchbench", pinchbench.stdout)

        clawbio = self.run_fleet(
            "--taskset", "clawbio", "--agent", "openclaw", "--workers", "5",
            "--dry-run",
        )
        self.assertEqual(clawbio.returncode, 0, clawbio.stderr)
        self.assertIn("Command: env COUNT=5 bash", clawbio.stdout)
        self.assertIn("run-openclaw-clawbio.sh", clawbio.stdout)
        self.assertNotIn("runner=clawbio", clawbio.stdout)

    def test_help_exposes_only_router_options(self):
        result = self.run_fleet("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--taskset", result.stdout)
        self.assertIn("--agent", result.stdout)
        self.assertIn("--workers", result.stdout)
        self.assertIn("--detach", result.stdout)
        self.assertIn("--spec", result.stdout)
        self.assertIn("--prompt", result.stdout)
        self.assertIn("--task", result.stdout)
        self.assertNotIn("--batch", result.stdout)
        self.assertIn("--dry-run", result.stdout)
        self.assertNotRegex(result.stdout, r"--tasks(?:\s|$)")
        # Help must be self-sufficient: a user who forgot the value names
        # should find them here without opening the README.
        self.assertIn("Short flags:", result.stdout)
        self.assertIn("terminalbench21", result.stdout)
        self.assertIn("claude-code", result.stdout)
        self.assertNotIn("Terminus-2", result.stdout)
        self.assertIn("Examples:", result.stdout)

    def test_misordered_prompt_reports_first_argument_requirement(self):
        result = self.run_fleet("--dry-run", "--prompt", "Run pinchbench")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--prompt must be the first argument", result.stderr)

    def test_spec_array_routes_multiple_runs_automatically(self):
        spec = self.root / "fleet-specs.json"
        spec.write_text(
            json.dumps(
                [
                    {"schema_version": 1, "taskset": "owner/first"},
                    {"schema_version": 1, "taskset": "owner/second"},
                ]
            ),
            encoding="utf-8",
        )

        result = self.run_fleet("--spec", str(spec), "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATASET_NAME=owner/first", result.stdout)
        self.assertIn("DATASET_NAME=owner/second", result.stdout)
        self.assertEqual(result.stdout.count("Command: env"), 2)
        self.assertIn("RUN_ID=fleet-", result.stderr)
        self.assertNotIn("runner=harbor", result.stdout)

    def test_multiple_spec_files_are_flattened_and_saved_as_an_array(self):
        first = self.root / "first.json"
        second = self.root / "second.json"
        output = self.root / "normalized.json"
        first.write_text(
            json.dumps({"schema_version": 1, "taskset": "owner/first"}),
            encoding="utf-8",
        )
        second.write_text(
            json.dumps(
                [
                    {"schema_version": 1, "taskset": "owner/second"},
                    {"schema_version": 1, "taskset": "owner/third", "workers": 3.0},
                ]
            ),
            encoding="utf-8",
        )

        result = self.run_fleet(
            "--spec", str(first), str(second),
            "--output", str(output), "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            [
                {"schema_version": 1, "taskset": "owner/first"},
                {"schema_version": 1, "taskset": "owner/second"},
                {"schema_version": 1, "taskset": "owner/third", "workers": 3},
            ],
        )
        self.assertEqual(result.stdout.count("Command: env"), 3)

    def test_single_element_array_keeps_single_run_semantics(self):
        spec = self.root / "one.json"
        output = self.root / "normalized.json"
        spec.write_text(
            json.dumps(
                [{"schema_version": 1, "taskset": "terminalbench21", "workers": 2}]
            ),
            encoding="utf-8",
        )

        result = self.run_fleet(
            "--spec", str(spec), "--output", str(output), "--dry-run"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            {"schema_version": 1, "taskset": "terminalbench21", "workers": 2},
        )
        self.assertNotIn("RUN_ID=fleet-", result.stderr)
        self.assertFalse((self.root / "fleet-batch-logs").exists())

    def test_invalid_file_prevents_all_multi_run_launches(self):
        valid = self.root / "valid.json"
        invalid = self.root / "invalid.json"
        valid.write_text(
            json.dumps({"schema_version": 1, "taskset": "owner/valid"}),
            encoding="utf-8",
        )
        invalid.write_text(
            json.dumps({"schema_version": 1, "taskset": ""}), encoding="utf-8"
        )

        result = self.run_fleet("--spec", str(valid), str(invalid))

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid FleetSpec v1", result.stderr)
        self.assertNotIn("runner=", result.stdout)
        self.assertFalse((self.root / "fleet-batch-logs").exists())

    def test_spec_stdin_cannot_be_combined_with_file_inputs(self):
        spec = self.root / "fleet-spec.json"
        spec.write_text(
            json.dumps({"schema_version": 1, "taskset": "terminalbench21"}),
            encoding="utf-8",
        )

        result = self.run_fleet(
            "--spec", "-", str(spec),
            input_text=json.dumps({"schema_version": 1, "taskset": "pinchbench"}),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)

    def test_spec_keeps_existing_option_order_compatibility(self):
        spec = self.root / "fleet-spec.json"
        spec.write_text(
            json.dumps({"schema_version": 1, "taskset": "terminalbench21"}),
            encoding="utf-8",
        )

        result = self.run_fleet("--dry-run", "--spec", str(spec))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATASET_NAME=terminalbench21", result.stdout)

    def test_removed_batch_flag_is_not_accepted(self):
        result = self.run_fleet("--batch", "runs.json", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("runner=", result.stdout)
        self.assertNotIn("--batch", result.stderr)

    def test_short_flags_match_long_forms(self):
        result = self.run_fleet(
            "-t",
            "terminal-bench/terminal-bench-2-1",
            "-a",
            "claude-code",
            "-n",
            "3",
            "-d",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATASET_NAME=terminal-bench/terminal-bench-2-1", result.stdout)
        self.assertIn("AGENT=claude-code", result.stdout)
        self.assertIn("TOTAL_WORKERS=3", result.stdout)
        self.assertIn("Harbor/start.sh --detach", result.stdout)

    def test_short_spec_flag_reads_stdin(self):
        result = self.run_fleet(
            "-s",
            "-",
            "--dry-run",
            input_text=json.dumps(
                {"schema_version": 1, "taskset": "terminalbench21"}
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DATASET_NAME=terminalbench21", result.stdout)

    def test_misordered_short_prompt_reports_first_argument_requirement(self):
        result = self.run_fleet("--dry-run", "-p", "Run pinchbench")

        self.assertEqual(result.returncode, 2)
        self.assertIn("-p must be the first argument", result.stderr)

    def test_portal_is_shell_only(self):
        self.assertFalse((SCRIPT.parent / "run_fleet.py").exists())
        self.assertFalse((SCRIPT.parent / "run_fleet_legacy.sh").exists())


if __name__ == "__main__":
    unittest.main()
