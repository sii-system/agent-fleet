import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run-parallel-workers.py"
)


def load_runner_module():
    spec = importlib.util.spec_from_file_location("run_parallel_workers", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WorkerMountTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner_module()

    def test_worker_mounts_state_outside_openclaw_home(self):
        docker_cmd = self.runner.build_worker_docker_command(
            image="pinchbench-runner:local",
            instance_index=1,
            container_prefix="fleet",
            token="fleet-token",
            openrouter_key="router-key",
            openai_api_key="custom-key",
            model_provider="openai-compatible",
            uv_cache_dir=Path("/tmp/uv-cache"),
            pinchbench_dir=Path("/tmp/pinchbench-skill"),
            worker_dir=Path("/tmp/worker"),
            config_dir=Path("/tmp/config"),
            workspace_dir=Path("/tmp/openclaw-workspace"),
            plugin_cache_dir=None,
            results_dir=Path("/tmp/results"),
            opik_state_dir=Path("/tmp/opik-state"),
            bench_cmd="echo test",
        )

        command = " ".join(docker_cmd)
        self.assertIn("-v /tmp/pinchbench-skill:/workspace", command)
        self.assertIn("-v /tmp/uv-cache:/home/node/.cache/uv", command)
        self.assertNotIn("-v /tmp/config:/home/node/.openclaw", command)
        self.assertIn("-v /tmp/config:/home/node/openclaw-state", command)
        self.assertIn("-v /tmp/openclaw-workspace:/home/node/workspace", command)
        self.assertIn("-v /tmp/opik-state:/home/node/pinchbench-opik-state", command)
        self.assertNotIn("/opt/openclaw-plugins/openclaw-opik-tracer:ro", command)
        self.assertIn("-e OPENCLAW_STATE_DIR=/home/node/openclaw-state", command)
        self.assertIn(
            "-e OPENCLAW_CONFIG_PATH=/home/node/openclaw-state/openclaw.json",
            command,
        )
        self.assertNotIn(":/home/node/.openclaw", command)

    def test_trace_off_worker_omits_opik_state_mount(self):
        docker_cmd = self.runner.build_worker_docker_command(
            image="pinchbench-runner:local",
            instance_index=1,
            container_prefix="fleet",
            token="fleet-token",
            openrouter_key="router-key",
            openai_api_key="custom-key",
            model_provider="openai-compatible",
            uv_cache_dir=Path("/tmp/uv-cache"),
            pinchbench_dir=Path("/tmp/pinchbench-skill"),
            worker_dir=Path("/tmp/worker"),
            config_dir=Path("/tmp/config"),
            workspace_dir=Path("/tmp/openclaw-workspace"),
            plugin_cache_dir=None,
            results_dir=Path("/tmp/results"),
            opik_state_dir=None,
            bench_cmd="echo test",
            container_env={"TRACE_TO_OPIK": "false"},
        )

        command = " ".join(docker_cmd)
        self.assertNotIn("pinchbench-opik-state", command)
        self.assertIn("-e TRACE_TO_OPIK=false", command)


if __name__ == "__main__":
    unittest.main()
