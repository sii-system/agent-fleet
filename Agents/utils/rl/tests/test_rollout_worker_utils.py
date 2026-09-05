"""Tests for rollout-only worker maintenance helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "rollout_worker_utils.py"
WORKER_SCRIPT = SCRIPT.with_name("run_rl_rollout_worker.sh")
SPEC = importlib.util.spec_from_file_location("rollout_worker_utils", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RolloutWorkerUtilsTest(unittest.TestCase):
    def test_json_path_helpers(self) -> None:
        payload = {"task": {"id": "task-a"}, "enabled": False, "items": [1, 2]}
        self.assertEqual(MODULE.json_path(payload, "task.id"), "task-a")
        self.assertEqual(MODULE.first_json_path(payload, ["missing", "enabled", "items"]), "false")

    def test_build_llm_kwargs_preserves_numeric_and_header_values(self) -> None:
        rendered = MODULE.build_llm_kwargs(
            ["0.5", "", "12", "0.1", "30", "2"],
            '{"X-Route-Key":"deployment-a"}',
        )

        self.assertEqual(
            json.loads(rendered),
            {
                "temperature": 0.5,
                "top_k": 12,
                "min_p": 0.1,
                "timeout": 30.0,
                "max_retries": 2,
                "extra_headers": {"X-Route-Key": "deployment-a"},
            },
        )

    def test_build_result_merges_harbor_result_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            request = directory / "request.json"
            result = directory / "result.json"
            output = directory / "output.json"
            request.write_text(
                '{"request_id":"request-1","task_id":"task-a"}',
                encoding="utf-8",
            )
            result.write_text(
                '{"agent_result":{"metadata":{"n_episodes":3}},'
                '"verifier_result":{"reward":1}}',
                encoding="utf-8",
            )

            MODULE.build_result(
                request,
                result,
                "/tmp/console.log",
                "1.0",
                "",
                0,
                output,
                "completed",
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["reward"], 1.0)
            self.assertEqual(payload["num_turns"], 3)
            self.assertEqual(payload["metadata"]["request_id"], "request-1")

    def test_benchmark_result_overrides_reward_but_preserves_harbor_reward(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            request = directory / "request.json"
            result = directory / "result.json"
            benchmark = directory / "benchmark.json"
            output = directory / "output.json"
            request.write_text('{"request_id":"request-2","task_id":"q1"}', encoding="utf-8")
            result.write_text('{"agent_result":{},"verifier_result":{"reward":1}}', encoding="utf-8")
            benchmark.write_text('{"benchmark":"browsecomp-plus","reward":0,"correct":false}', encoding="utf-8")

            MODULE.build_result(request, result, "/tmp/log", "1", "", 0, output, "completed", benchmark)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["reward"], 0.0)
            self.assertEqual(payload["harbor_reward"], 1.0)
            self.assertEqual(payload["benchmark_result"]["benchmark"], "browsecomp-plus")
            self.assertEqual(payload["status"], "completed")

    def test_worker_passes_original_harbor_reward_to_result_builder(self) -> None:
        worker = WORKER_SCRIPT.read_text(encoding="utf-8")
        build_line = next(
            line for line in worker.splitlines() if line.strip().startswith("json_build_result ")
        )
        self.assertIn('"$harbor_reward"', build_line)
        self.assertNotIn('"$reward"', build_line)

    def test_benchmark_reward_contract_rejects_mismatch_and_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "benchmark.json"
            path.write_text('{"benchmark":"browsecomp-plus","reward":1}', encoding="utf-8")
            self.assertEqual(MODULE.read_benchmark_reward(path, "browsecomp-plus"), 1.0)
            with self.assertRaises(ValueError):
                MODULE.read_benchmark_reward(path, "another-benchmark")
            path.write_text('{"benchmark":"browsecomp-plus","reward":"1"}', encoding="utf-8")
            with self.assertRaises(TypeError):
                MODULE.read_benchmark_reward(path)

    def test_build_request_headers_merges_deployment_and_session_headers(self) -> None:
        headers = MODULE.build_request_headers(
            '{"version":1,"headers":{"set":{"X-Route-Key":"deployment-a"}}}',
            "session-1",
        )

        self.assertEqual(
            headers,
            {
                "X-Route-Key": "deployment-a",
                "X-Session-Id": "session-1",
                "Proxy-X-Session-Id": "session-1",
            },
        )

    def test_build_request_headers_rejects_unsupported_or_unsafe_config(self) -> None:
        invalid_values = (
            "[]",
            "{}",
            '{"version":2}',
            '{"version":1,"body":{"set":{}}}',
            '{"version":1,"headers":{"remove":[]}}',
            '{"version":1,"headers":{"set":{"Bad Header":"value"}}}',
            '{"version":1,"headers":{"set":{"X-Test":1}}}',
            '{"version":1,"headers":{"set":{"X-Test":"safe\\r\\nunsafe"}}}',
            '{"version":1,"headers":{"set":{"X-Test":"a","x-test":"b"}}}',
            '{"version":1,"headers":{"set":{"X-Session-Id":"spoofed"}}}',
        )
        for raw in invalid_values:
            with self.subTest(raw=raw), self.assertRaises(
                (TypeError, ValueError, json.JSONDecodeError)
            ):
                MODULE.build_request_headers(raw)

    def test_render_header_lines_replaces_managed_names_only(self) -> None:
        rendered = MODULE.render_header_lines(
            "Existing: kept\nx-route-key: stale",
            {"X-Route-Key": "deployment-a", "X-Session-Id": "session-1"},
        )

        self.assertEqual(
            rendered.splitlines(),
            [
                "Existing: kept",
                "X-Route-Key: deployment-a",
                "X-Session-Id: session-1",
            ],
        )

    def test_request_headers_cli_reads_host_environment(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "MODEL_REQUEST_CONFIG_JSON": (
                    '{"version":1,"headers":{"set":'
                    '{"X-Route-Key":"deployment-a"}}}'
                ),
                "MODEL_REQUEST_SESSION_ID": "session-1",
            }
        )
        result = subprocess.run(
            ["python3", str(SCRIPT), "request-headers"],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "X-Route-Key": "deployment-a",
                "X-Session-Id": "session-1",
                "Proxy-X-Session-Id": "session-1",
            },
        )

    def test_prune_trial_artifacts_keeps_newest_directories(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            worker_root = Path(root)
            for index in range(4):
                trial = worker_root / f"trial-{index}"
                trial.mkdir()
                (trial / "result.json").write_text("{}", encoding="utf-8")
                os.utime(trial, ns=(index + 1, index + 1))

            MODULE.prune_trial_artifacts(worker_root, keep=2)

            self.assertEqual(
                sorted(path.name for path in worker_root.iterdir()),
                ["trial-2", "trial-3"],
            )


if __name__ == "__main__":
    unittest.main()
