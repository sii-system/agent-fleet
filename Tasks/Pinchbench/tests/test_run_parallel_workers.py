import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run-parallel-workers.py"
)


def load_runner_module():
    spec = importlib.util.spec_from_file_location("run_parallel_workers", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BenchmarkCommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner_module()

    def _write_task(self, tasks_dir: Path, task_id: str, grading_type: str = "automated") -> None:
        (tasks_dir / f"{task_id}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"id: {task_id}",
                    f"name: {task_id}",
                    f"grading_type: {grading_type}",
                    "---",
                    "## Prompt",
                    "Test task.",
                ]
            ),
            encoding="utf-8",
        )

    def test_expand_suite_uses_manifest_order_for_all_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pinchbench_dir = Path(tmp)
            tasks_dir = pinchbench_dir / "tasks"
            tasks_dir.mkdir()
            for task_id in ("task_a", "task_b", "task_c"):
                self._write_task(tasks_dir, task_id)
            (tasks_dir / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "run_first:",
                        "  - task_c",
                        "categories:",
                        "  coding:",
                        "    - task_b",
                        "  research:",
                        "    - task_a",
                    ]
                ),
                encoding="utf-8",
            )

            task_ids = self.runner.expand_suite(pinchbench_dir, "all")

        self.assertEqual(task_ids, ["task_c", "task_b", "task_a"])

    def test_expand_suite_supports_manifest_category_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            pinchbench_dir = Path(tmp)
            tasks_dir = pinchbench_dir / "tasks"
            tasks_dir.mkdir()
            for task_id in ("task_a", "task_b", "task_c"):
                self._write_task(tasks_dir, task_id)
            (tasks_dir / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "categories:",
                        "  coding:",
                        "    - task_b",
                        "    - task_a",
                        "  research:",
                        "    - task_c",
                    ]
                ),
                encoding="utf-8",
            )

            task_ids = self.runner.expand_suite(pinchbench_dir, "coding")

        self.assertEqual(task_ids, ["task_b", "task_a"])

    def test_expand_suite_supports_manifest_core_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            pinchbench_dir = Path(tmp)
            tasks_dir = pinchbench_dir / "tasks"
            tasks_dir.mkdir()
            for task_id in ("task_a", "task_b", "task_c"):
                self._write_task(tasks_dir, task_id)
            (tasks_dir / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "core:",
                        "  - task_b",
                        "  - task_c",
                        "categories:",
                        "  coding:",
                        "    - task_a",
                        "    - task_b",
                        "  research:",
                        "    - task_c",
                    ]
                ),
                encoding="utf-8",
            )

            task_ids = self.runner.expand_suite(pinchbench_dir, "core")

        self.assertEqual(task_ids, ["task_b", "task_c"])

    def test_default_ref_tracks_latest_supported_pinchbench_commit(self):
        self.assertEqual(
            self.runner.DEFAULT_PINCHBENCH_REF,
            "f3f1cb560c252541cef6a106c05ba4f2e8068be0",
        )

    def test_run_command_requires_prepared_agent(self):
        command = self.runner.build_benchmark_command(
            model="openrouter/openai/gpt-oss-20b:free",
            model_provider="auto",
            base_url="",
            api_key="",
            suite_chunk="task_sanity,task_weather",
            judge="",
            upload_enabled=False,
            prepare_agent_only=False,
            require_prepared_agent=True,
            timeout_multiplier="1.0",
        )

        self.assertIn("--require-prepared-agent", command)
        self.assertIn("--suite task_sanity,task_weather", command)
        self.assertNotIn("--prepare-agent-only", command)

    def test_prepare_command_creates_agent_without_task_suite(self):
        command = self.runner.build_benchmark_command(
            model="openrouter/openai/gpt-oss-20b:free",
            model_provider="auto",
            base_url="",
            api_key="",
            suite_chunk="task_sanity",
            judge="",
            upload_enabled=False,
            prepare_agent_only=True,
            require_prepared_agent=False,
            timeout_multiplier="1.0",
        )

        self.assertIn("--prepare-agent-only", command)
        self.assertNotIn("--suite", command)
        self.assertNotIn("--output-dir /results", command)

    def test_command_includes_custom_endpoint_arguments(self):
        command = self.runner.build_benchmark_command(
            model="glm-5.1-fp8",
            model_provider="openai-compatible",
            base_url="https://example.invalid/v1",
            api_key="secret-key",
            suite_chunk="task_sanity",
            judge="",
            upload_enabled=False,
            prepare_agent_only=False,
            require_prepared_agent=True,
            timeout_multiplier="1.0",
        )

        self.assertIn("--base-url https://example.invalid/v1", command)
        self.assertIn("--api-key secret-key", command)

    def test_normalize_base_url_appends_v1_to_root(self):
        self.assertEqual(
            self.runner.normalize_base_url("https://example.invalid"),
            "https://example.invalid/v1",
        )

    def test_normalize_base_url_is_idempotent(self):
        for given in ("https://example.invalid/v1", "https://example.invalid/v1/"):
            self.assertEqual(
                self.runner.normalize_base_url(given), "https://example.invalid/v1"
            )

    def test_worker_container_exports_openai_api_key(self):
        docker_cmd = self.runner.build_worker_docker_command(
            image="pinchbench-runner:local",
            instance_index=1,
            container_prefix="openclaw",
            token="fleet-token",
            openrouter_key="router-key",
            openai_api_key="custom-key",
            model_provider="openai-compatible",
            uv_cache_dir=Path("/tmp/uv-cache"),
            pinchbench_dir=Path("/tmp/pinchbench-skill"),
            worker_dir=Path("/tmp/worker"),
            config_dir=Path("/tmp/config"),
            workspace_dir=Path("/tmp/workspace"),
            plugin_cache_dir=None,
            results_dir=Path("/tmp/results"),
            opik_state_dir=Path("/tmp/opik-state"),
            bench_cmd="echo test",
        )

        self.assertIn("-e", docker_cmd)
        self.assertIn("OPENAI_API_KEY=custom-key", docker_cmd)

    def test_worker_container_uses_configured_container_prefix(self):
        docker_cmd = self.runner.build_worker_docker_command(
            image="pinchbench-runner:local",
            instance_index=2,
            container_prefix="fleet",
            token="fleet-token",
            openrouter_key="router-key",
            openai_api_key="custom-key",
            model_provider="openai-compatible",
            uv_cache_dir=Path("/tmp/uv-cache"),
            pinchbench_dir=Path("/tmp/pinchbench-skill"),
            worker_dir=Path("/tmp/worker"),
            config_dir=Path("/tmp/config"),
            workspace_dir=Path("/tmp/workspace"),
            plugin_cache_dir=None,
            results_dir=Path("/tmp/results"),
            opik_state_dir=Path("/tmp/opik-state"),
            bench_cmd="echo test",
        )

        network_index = docker_cmd.index("--network")
        self.assertEqual(docker_cmd[network_index + 1], "container:fleet-2")

    def test_runner_config_reads_generated_container_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fleet_env = tmp_path / "fleet.env"
            generated_env = tmp_path / ".env"
            pinchbench_env = tmp_path / "pinchbench.env"
            fleet_env.write_text("MODEL=test-model\nCOUNT=2\n", encoding="utf-8")
            generated_env.write_text(
                "TOKEN_1=test-token\nCONTAINER_NAME_PREFIX=fleet\n",
                encoding="utf-8",
            )
            pinchbench_env.write_text("", encoding="utf-8")

            with mock.patch.object(self.runner, "FLEET_ENV_FILE", fleet_env), \
                 mock.patch.object(self.runner, "ENV_FILE", generated_env), \
                 mock.patch.object(self.runner, "PINCHBENCH_ENV_FILE", pinchbench_env), \
                 mock.patch.dict(self.runner.os.environ, {}, clear=True):
                config = self.runner.load_runner_config()

        self.assertEqual(config["CONTAINER_NAME_PREFIX"], "fleet")

    def test_runner_config_reads_shared_model_from_root_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_env = tmp_path / "config.env"
            config_local_env = tmp_path / "config.local.env"
            fleet_env = tmp_path / "fleet.env"
            generated_env = tmp_path / ".env"
            pinchbench_env = tmp_path / "pinchbench.env"
            config_env.write_text(
                "MODEL=shared-model\nTRACE_TO_OPIK=false\n",
                encoding="utf-8",
            )
            config_local_env.write_text("", encoding="utf-8")
            fleet_env.write_text("COUNT=2\n", encoding="utf-8")
            generated_env.write_text("", encoding="utf-8")
            pinchbench_env.write_text("TRACE_TO_OPIK=true\n", encoding="utf-8")

            with mock.patch.object(self.runner, "CONFIG_ENV_FILE", config_env), \
                 mock.patch.object(self.runner, "CONFIG_LOCAL_ENV_FILE", config_local_env), \
                 mock.patch.object(self.runner, "FLEET_ENV_FILE", fleet_env), \
                 mock.patch.object(self.runner, "ENV_FILE", generated_env), \
                 mock.patch.object(self.runner, "PINCHBENCH_ENV_FILE", pinchbench_env), \
                 mock.patch.dict(self.runner.os.environ, {}, clear=True):
                config = self.runner.load_runner_config()

        self.assertEqual(config["MODEL"], "shared-model")
        self.assertEqual(config["TRACE_TO_OPIK"], "false")

    def test_runner_config_resolves_relative_local_repo_url_from_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root = tmp_path / "agent-fleet"
            repo_root.mkdir()
            fleet_env = tmp_path / "fleet.env"
            generated_env = tmp_path / ".env"
            pinchbench_env = tmp_path / "pinchbench.env"
            fleet_env.write_text("MODEL=test-model\nCOUNT=2\n", encoding="utf-8")
            generated_env.write_text("CONTAINER_NAME_PREFIX=fleet\n", encoding="utf-8")
            pinchbench_env.write_text("PINCHBENCH_REPO_URL=../skills\n", encoding="utf-8")

            with mock.patch.object(self.runner, "REPO_ROOT", repo_root), \
                 mock.patch.object(self.runner, "FLEET_ENV_FILE", fleet_env), \
                 mock.patch.object(self.runner, "ENV_FILE", generated_env), \
                 mock.patch.object(self.runner, "PINCHBENCH_ENV_FILE", pinchbench_env), \
                 mock.patch.dict(self.runner.os.environ, {}, clear=True):
                config = self.runner.load_runner_config()

        self.assertEqual(
            config["PINCHBENCH_REPO_URL"],
            str((repo_root / "../skills").resolve()),
        )

    def test_trace_off_rejects_stale_gateway_opik_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_base = Path(tmp)
            instance_dir = config_base / "1"
            instance_dir.mkdir()
            (instance_dir / "openclaw.json").write_text(
                '{"plugins":{"allow":["openai","openclaw-opik-tracer"],'
                '"entries":{"openclaw-opik-tracer":{"enabled":true}}}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SystemExit,
                r"TRACE_TO_OPIK=false.*instance 1.*setup\.sh.*restart",
            ):
                self.runner.validate_trace_off_gateway_configs(config_base, 1)

    def test_trace_off_accepts_gateway_without_opik_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_base = Path(tmp)
            instance_dir = config_base / "1"
            instance_dir.mkdir()
            (instance_dir / "openclaw.json").write_text(
                '{"plugins":{"allow":["openai"],"entries":{}}}',
                encoding="utf-8",
            )

            self.runner.validate_trace_off_gateway_configs(config_base, 1)

    def test_worker_command_uses_private_home_without_su_or_root_uv_copy(self):
        command = self.runner.wrap_worker_command(
            run_as_user="node",
            bench_cmd="set -euo pipefail\ncd /runner\n/tmp/uv run /workspace/scripts/benchmark.py",
        )

        self.assertIn("install -m 0755 /root/.local/bin/uv /tmp/uv", command)
        self.assertIn("mkdir -p /home/node/.openclaw", command)
        self.assertIn("PINCHBENCH_RESET_GATEWAY_OPIK_STATE", command)
        self.assertIn("opik_tracer_state.json", command)
        self.assertIn("ln -sfn /home/node/openclaw-state/agents /home/node/.openclaw/agents", command)
        self.assertIn("ln -sfn /home/node/pinchbench-opik-state /home/node/.openclaw/state", command)
        self.assertIn("touch /runner/benchmark.log", command)
        self.assertIn("chown node:node /runner/benchmark.log", command)
        self.assertIn("su node -s /bin/bash -c", command)
        self.assertIn("PINCHBENCH_OPIK_DRAIN_SECONDS", command)
        self.assertIn("openclaw_opik_tracer.py", command)

    def test_trace_off_worker_command_skips_opik_state_and_drain(self):
        command = self.runner.wrap_worker_command(
            run_as_user="node",
            bench_cmd="echo test",
            tracing_enabled=False,
        )

        self.assertNotIn("PINCHBENCH_RESET_GATEWAY_OPIK_STATE", command)
        self.assertNotIn("pinchbench-opik-state", command)
        self.assertNotIn("PINCHBENCH_OPIK_DRAIN_SECONDS", command)
        self.assertNotIn("openclaw_opik_tracer.py", command)

    def test_worker_command_does_not_chown_openclaw_state_or_workspace(self):
        command = self.runner.wrap_worker_command(
            run_as_user="node",
            bench_cmd="set -euo pipefail\ncd /runner\n/tmp/uv run /workspace/scripts/benchmark.py",
        )

        self.assertIn("chown -R node:node", command)

    def test_worker_image_installs_opik_tracer_runtime(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn("/opt/opik-venv", dockerfile)
        self.assertIn("ARG OPENCLAW_BASE_IMAGE=openclaw:local-opik", dockerfile)
        self.assertIn("FROM ${OPENCLAW_BASE_IMAGE}", dockerfile)
        self.assertIn('ARG TRACE_TO_OPIK=true', dockerfile)
        self.assertIn("/opt/pinchbench-trace-to-opik", dockerfile)
        self.assertNotIn("cache/" + "sii-opik", dockerfile)
        self.assertIn("opik>=1.0.0", dockerfile)
        self.assertIn("uuid6", dockerfile)
        self.assertIn("socksio", dockerfile)

    def test_trace_off_image_path_uses_local_base_without_opik_probe(self):
        config = {"PINCHBENCH_DOCKER_IMAGE": "pinchbench-runner:local"}

        build_command = self.runner.worker_image_build_command(
            config,
            tracing_enabled=False,
        )
        probe_command = self.runner.worker_image_probe_command(
            config["PINCHBENCH_DOCKER_IMAGE"],
            tracing_enabled=False,
        )

        self.assertIn("TRACE_TO_OPIK=false", build_command)
        self.assertIn("OPENCLAW_BASE_IMAGE=openclaw:local", build_command)
        self.assertIn("pinchbench-trace-to-opik", probe_command[-1])
        self.assertIn("test ! -e /opt/opik-venv", probe_command[-1])
        self.assertIn("test ! -e /opt/openclaw-plugins/openclaw-opik-tracer", probe_command[-1])

    def test_trace_on_image_path_keeps_opik_base_and_probe(self):
        config = {"PINCHBENCH_DOCKER_IMAGE": "pinchbench-runner:local"}

        build_command = self.runner.worker_image_build_command(
            config,
            tracing_enabled=True,
        )
        probe_command = self.runner.worker_image_probe_command(
            config["PINCHBENCH_DOCKER_IMAGE"],
            tracing_enabled=True,
        )

        self.assertIn("TRACE_TO_OPIK=true", build_command)
        self.assertIn("OPENCLAW_BASE_IMAGE=openclaw:local-opik", build_command)
        self.assertIn("pinchbench-trace-to-opik", probe_command[-1])
        self.assertIn("opik-venv", probe_command[-1])
        self.assertIn("openclaw-opik-tracer", probe_command[-1])

    def test_classify_web_search_disabled_timeout_as_skipped(self):
        merged = {
            "tasks": [
                {
                    "task_id": "task_polymarket_briefing",
                    "status": "timeout",
                    "error": "",
                    "transcript_path": "/tmp/task_polymarket_briefing.jsonl",
                }
            ]
        }
        transcript = (
            '{"type":"message","message":{"role":"toolResult","content":[{"type":"text",'
            '"text":"{\\"status\\": \\"error\\", \\"tool\\": \\"web_search\\", '
            '\\"error\\": \\"web_search is disabled or no provider is available.\\"}"}]}}\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "task_polymarket_briefing.jsonl"
            transcript_path.write_text(transcript, encoding="utf-8")
            merged["tasks"][0]["transcript_path"] = str(transcript_path)

            normalized = self.runner.normalize_task_results_for_summary(merged)

        self.assertEqual(normalized["tasks"][0]["status"], "skipped_web_search_disabled")
        self.assertEqual(normalized["tasks"][0]["skip_reason"], "web_search_disabled")

    def test_validate_iteration_completion_treats_skipped_web_search_disabled_as_terminal(self):
        merged = {
            "tasks": [
                {
                    "task_id": "task_polymarket_briefing",
                    "status": "skipped_web_search_disabled",
                    "skip_reason": "web_search_disabled",
                }
            ]
        }

        completion = self.runner.validate_iteration_completion(
            merged,
            ["task_polymarket_briefing"],
        )

        self.assertTrue(completion["ok"])

    def test_summarize_iteration_counts_skipped_web_search_disabled_separately(self):
        merged = {
            "tasks": [
                {
                    "task_id": "task_polymarket_briefing",
                    "status": "skipped_web_search_disabled",
                    "skip_reason": "web_search_disabled",
                },
                {
                    "task_id": "task_sanity",
                    "status": "success",
                    "grading": {"mean": 1.0},
                },
            ]
        }

        summary = self.runner.summarize_iteration(
            iteration=1,
            merged=merged,
            run_dir=Path("/tmp"),
            started_at=self.runner.datetime.now(),
            completion={
                "ok": True,
                "expected_count": 2,
                "actual_count": 2,
                "missing_task_ids": [],
                "non_terminal_task_ids": [],
            },
        )

        self.assertEqual(summary["task_count"], 2)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["skipped_web_search_disabled_count"], 1)

    def test_summarize_iteration_preserves_naive_timestamp_format(self):
        timestamp = datetime(  # noqa: DTZ001 - verifies legacy naive output contract
            2026, 7, 25, 12, 34, 56
        )
        with mock.patch.object(self.runner, "datetime") as datetime_type:
            datetime_type.now.return_value = timestamp
            summary = self.runner.summarize_iteration(
                iteration=1,
                merged={"tasks": []},
                run_dir=Path("/tmp"),
                started_at=timestamp,
                completion={
                    "ok": True,
                    "expected_count": 0,
                    "actual_count": 0,
                    "missing_task_ids": [],
                    "non_terminal_task_ids": [],
                },
            )

        self.assertEqual(summary["started_at"], "2026-07-25T12:34:56")
        self.assertEqual(summary["finished_at"], "2026-07-25T12:34:56")


if __name__ == "__main__":
    unittest.main()
