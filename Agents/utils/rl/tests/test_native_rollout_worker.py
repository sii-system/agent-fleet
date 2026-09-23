"""Request isolation and bounded native rollout, using the installed Harbor API."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_rollout_worker import TrialConfig, configure_trial, serve


class NativeRolloutTest(unittest.TestCase):
    def setUp(self):
        self.host = patch.dict(os.environ, RL_DATASET_NAME="swe", OPIK_PROJECT_NAME="test",
                               MODEL_REQUEST_CONFIG_JSON='{"version":1,"headers":{"set":{"X-Backend":"gpu:18081"}}}')
        self.host.start()
        self.addCleanup(self.host.stop)
        self.template = TrialConfig.model_validate({
            "task": {"path": "/tasks/original"},
            "agent": {"name": "claude-code", "model_name": "old", "kwargs": {"llm_kwargs": {"timeout": 900}},
                      "env": {"ANTHROPIC_AUTH_TOKEN": "unit-test-only", "CC_OPIK_ENABLE_HOOK": "false"}},
            "environment": {"import_path": "yicloud_opensandbox:YiCloudOpenSandboxEnvironment"},
        })

    def request(self, identity="one"):
        return {"request_id": identity, "request_file_id": identity, "task_id": identity,
                "task_path": "/tasks/" + identity, "session_id": "session-" + identity,
                "ray_submission_id": "ray", "dataset_name": "swe", "opik_project_name": "test",
                "model_name": identity, "api_base": "http://model-" + identity + "/v1",
                "environment_type": "opensandbox"}

    def test_launcher_starts_without_pythonpath_or_opik(self):
        script = Path(__file__).resolve().parents[1] / "run_rl_rollout_worker.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(self.template.model_dump_json())
            queue = root / "queue"
            (queue / "pending").mkdir(parents=True)
            # Exercise admission without creating a sandbox or calling a model.
            (queue / "pending/one.json").write_text(json.dumps(
                self.request() | {"environment_type": "docker"}
            ))
            result_file = queue / "results/one.json"
            # A clean child environment must not inherit CI's PYTHONPATH or site config.
            env = {
                "PATH": os.environ["PATH"], "HOME": str(root),
                "AGENT_FLEET_CONFIG_LOADED_ROOT": str(script.parents[3]),
                "ROLLOUT": "1", "HARBOR_NATIVE_CONCURRENCY": "1",
                "HARBOR_OPIK_PYTHON": sys.executable, "OPIK_URL": "",
                "RL_NATIVE_TRIAL_CONFIG": str(config), "RL_MAX_CONCURRENT": "2",
                "RL_QUEUE_DIR": str(queue), "JOBS_ROOT": str(root / "trials"),
                "OUTPUT_ROOT": str(root / "output"),
            }
            worker = subprocess.Popen(
                ["bash", str(script), "1"], cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                deadline = time.monotonic() + 15
                while not result_file.is_file() and worker.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                ready = result_file.is_file()
                if worker.poll() is None:
                    worker.terminate()
                output, _ = worker.communicate(timeout=10)
                self.assertTrue(ready, output)
                self.assertEqual(worker.returncode, 0, output)
                result = json.loads(result_file.read_text())
                self.assertFalse(result["ok"])
                self.assertEqual(result["exception_info"]["exception_type"], "ValueError")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()

    def test_isolated_model_session_and_original_config(self):
        before = self.template.model_dump_json()
        first = configure_trial(self.template, self.request(), Path("/trials"))
        second = configure_trial(self.template, self.request("two"), Path("/trials"))
        self.assertEqual(first.agent.env["ANTHROPIC_BASE_URL"], "http://model-one")
        self.assertEqual(second.agent.env["ANTHROPIC_MODEL"], "two")
        self.assertIn("X-Session-Id: session-one", first.agent.env["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertNotIn("session-two", first.agent.env["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertEqual(first.agent.env["ANTHROPIC_AUTH_TOKEN"], "unit-test-only")
        self.assertEqual(self.template.model_dump_json(), before)
        self.assertEqual(first.job_id, second.job_id)

    def test_request_budgets_and_sampling(self):
        request = self.request() | {"max_turns": "64", "temperature": 0.7, "top_p": 0.9,
                                   "collect_rollout_details": "true", "force_build": "false",
                                   "llm_timeout": 100, "llm_max_retries": 0, "claude_code_max_output_tokens": 2048}
        config = configure_trial(self.template, request, Path("/trials"))
        self.assertEqual(config.agent.kwargs["max_turns"], 64)
        self.assertEqual(config.agent.kwargs["llm_kwargs"]["temperature"], 0.7)
        self.assertEqual(config.agent.env["API_TIMEOUT_MS"], "100000")
        self.assertIs(config.agent.kwargs["collect_rollout_details"], True)
        self.assertFalse(config.environment.force_build)

    def test_reject_incompatible_worker_identity(self):
        for change in ({"environment_type": "docker"}, {"dataset_name": "other"}, {"opik_project_name": "other"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                configure_trial(self.template, self.request() | change, Path("/trials"))

    def test_refuses_unreconciled_active_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "active").mkdir()
            (root / "active/old.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "reconcile"):
                asyncio.run(serve(self.template, root, root / "trials", 2))

    def test_trial_failure_is_published_not_reported_as_reward_zero_success(self):
        async def exercise(root, missing_result):
            class Queue:
                def __init__(self, **kwargs):
                    pass

                async def submit(self, config):
                    if missing_result:
                        return
                    raise TimeoutError("unit-test trial failure")

            (root / "pending").mkdir()
            (root / "pending/one.json").write_text(json.dumps(self.request()))
            with patch("native_rollout_worker.TrialQueue", Queue):
                worker = asyncio.create_task(serve(self.template, root, root / "trials", 1))
                try:
                    output = root / "results/one.json"
                    for _ in range(100):
                        if output.exists():
                            break
                        await asyncio.sleep(0.02)
                    result = json.loads(output.read_text())
                    self.assertFalse(result["ok"])
                    self.assertIn("RuntimeError" if missing_result else "TimeoutError", json.dumps(result))
                    self.assertFalse((root / "active/one.json").exists())
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

        for missing_result in (False, True):
            with self.subTest(missing_result=missing_result), tempfile.TemporaryDirectory() as directory:
                asyncio.run(exercise(Path(directory), missing_result))

    def test_queue_preserves_result_contract_and_bounds_admission(self):
        async def exercise(root):
            from types import SimpleNamespace
            running = peak = completed = 0

            class Queue:
                def __init__(self, **kwargs):
                    self.limit = kwargs["n_concurrent"]
                    assert kwargs["retry_config"].max_retries == 0

                async def submit(self, config):
                    nonlocal running, peak, completed
                    running += 1
                    peak = max(peak, running)
                    await asyncio.sleep(0.02)
                    target = config.trials_dir / config.trial_name
                    target.mkdir(parents=True)
                    (target / "result.json").write_text(json.dumps({"agent_result": {}, "verifier_result": {"rewards": {"reward": 0}}}))
                    running -= 1
                    completed += 1
                    return SimpleNamespace(exception_info=None, verifier_result=SimpleNamespace(rewards={"reward": 0}))

            (root / "pending").mkdir()
            for index in range(5):
                (root / "pending" / f"{index}.json").write_text(json.dumps(self.request(str(index))))
            with patch("native_rollout_worker.TrialQueue", Queue):
                worker = asyncio.create_task(serve(self.template, root, root / "trials", 2))
                try:
                    for _ in range(100):
                        if len(list((root / "results").glob("*.json"))) == 5:
                            break
                        await asyncio.sleep(0.05)
                    self.assertEqual(completed, 5)
                    self.assertEqual(peak, 2)
                    for path in (root / "results").glob("*.json"):
                        result = json.loads(path.read_text())
                        self.assertTrue(result["ok"])
                        self.assertEqual(result["reward"], 0)
                    self.assertEqual(list((root / "active").glob("*.json")), [])
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    unittest.main()
