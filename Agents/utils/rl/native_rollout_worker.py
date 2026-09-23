"""Consume the existing rollout queue with one native Harbor TrialQueue.

The host supplies a prepared Harbor TrialConfig, including cached runtime mounts.
Request overrides are copied per trial, never written to process environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from harbor.models.job.config import RetryConfig
from harbor.models.trial.config import TrialConfig
from harbor.trial.queue import TrialQueue
from rollout_worker_utils import (
    build_llm_kwargs,
    build_request_headers,
    build_result,
    render_header_lines,
)

LOG = logging.getLogger(__name__)


def configure_trial(template: TrialConfig, request: dict, trials_dir: Path) -> TrialConfig:
    """Keep the prepared runtime/verifier; isolate task and model settings."""
    if template.agent.name != "claude-code" or request["environment_type"] != "opensandbox":
        raise ValueError("Native rollout currently supports Claude Code with OpenSandbox")
    if request["dataset_name"] != os.environ["RL_DATASET_NAME"]:
        raise ValueError("Native rollout requires the prepared dataset/verifier runtime")
    if request["opik_project_name"] != os.environ.get("OPIK_PROJECT_NAME", ""):
        raise ValueError("Native rollout requires one Opik project per worker")
    data = template.model_dump(mode="python", context={"redact_sensitive_env": False})
    data.update(task={"path": request["task_path"]}, trial_name=request["request_file_id"],
                trials_dir=trials_dir, job_id=uuid5(NAMESPACE_URL, request["ray_submission_id"]))
    agent = data["agent"]
    agent["n_concurrent"] = None
    agent["model_name"] = model = request["model_name"]
    env = agent["env"]
    env.update(HARBOR_TASK_ID=request["task_id"], HARBOR_RUN_ID=request["ray_submission_id"],
               HARBOR_DATASET=request["dataset_name"], OPIK_PROJECT_NAME=request["opik_project_name"])
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[key] = model
    headers = build_request_headers(os.environ.get("MODEL_REQUEST_CONFIG_JSON", ""), request["session_id"])
    env["ANTHROPIC_CUSTOM_HEADERS"] = render_header_lines(env.get("ANTHROPIC_CUSTOM_HEADERS", ""), headers)
    kwargs = agent["kwargs"]
    if request.get("api_base"):
        base = request["api_base"].rstrip("/").removesuffix("/chat/completions").removesuffix("/v1")
        kwargs["api_base"] = base + "/v1/chat/completions"
        env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL") or base
    kwargs.setdefault("llm_kwargs", {}).update(json.loads(build_llm_kwargs([
        str(request.get(key, os.environ.get(f"RL_{key.upper()}", "")))
        for key in ("temperature", "top_p", "top_k", "min_p", "llm_timeout", "llm_max_retries")
    ], json.dumps(headers))))
    for key in ("max_new_tokens", "max_turns", "model_info", "collect_rollout_details", "enable_summarize"):
        if key in request:
            value = request[key]
            if isinstance(value, str):
                value = json.loads(value)
            kwargs[key] = value
    if "claude_code_max_output_tokens" in request:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request["claude_code_max_output_tokens"])
    for key, target, factor in (("llm_timeout", "API_TIMEOUT_MS", 1000),
                                 ("llm_max_retries", "CLAUDE_CODE_MAX_RETRIES", 1)):
        if key in request:
            env[target] = str(int(float(request[key]) * factor))
    if "agent_timeout_multiplier" in request:
        data["agent_timeout_multiplier"] = float(request["agent_timeout_multiplier"])
    if "force_build" in request:
        data["environment"]["force_build"] = str(request["force_build"]).lower() in {"1", "true", "yes", "on"}
    return TrialConfig.model_validate(data)


async def serve(template: TrialConfig, root: Path, trials: Path, limit: int) -> None:
    queue = TrialQueue(n_concurrent=limit, retry_config=RetryConfig(max_retries=0))
    pending, active, results = (root / name for name in ("pending", "active", "results"))
    for directory in (pending, active, results, trials):
        directory.mkdir(parents=True, exist_ok=True)
    if any(active.glob("*.json")):
        raise RuntimeError("Unfinished requests exist; reconcile their sandboxes before restarting")
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopping.set)

    async def run(path: Path) -> None:
        request = json.loads(path.read_text())
        output = results / path.name
        result_file = None
        reward, error, code = "", "", 0
        try:
            config = configure_trial(template, request, trials)
            LOG.info("start request=%s task=%s", request["request_id"], request["task_id"])
            result = await queue.submit(config)
            result_file = trials / config.trial_name / "result.json"
            if not result_file.is_file():
                raise RuntimeError("Harbor returned without persisting the trial result")
            if result.verifier_result and result.verifier_result.rewards:
                reward = str(result.verifier_result.rewards.get("reward", ""))
            if result.exception_info:
                error = result.exception_info.exception_type
        except Exception as exc:
            LOG.exception("Native rollout request failed: %s", request["request_id"])
            error, code = type(exc).__name__, 1
        build_result(path, result_file, str(trials / request["request_file_id"] / "trial.log"),
                     reward, error, code, output, "failed" if error else "completed")
        path.unlink()
        LOG.info("finish request=%s reward=%s exception=%s", request["request_id"], reward, error)

    tasks: set[asyncio.Task] = set()
    try:
        while not stopping.is_set():
            for task in list(tasks):
                if task.done():
                    tasks.remove(task)
                    task.result()
            for path in pending.glob("*.json"):
                if len(tasks) >= limit:
                    break
                target = active / path.name
                path.rename(target)
                tasks.add(asyncio.create_task(run(target)))
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
    finally:
        # Stop admission first; keep native cleanup/result persistence for in-flight trials.
        if tasks:
            await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    template = TrialConfig.model_validate_json(Path(os.environ["RL_NATIVE_TRIAL_CONFIG"]).read_text())
    if template.install_only or template.verifier.disable:
        raise ValueError("Native RL requires agent execution and verification")
    if template.environment.import_path != "yicloud_opensandbox:YiCloudOpenSandboxEnvironment":
        raise ValueError("Native RL requires the prepared YiCloud OpenSandbox provider")
    if template.agent.env.get("CC_OPIK_ENABLE_HOOK", "false").lower() != "false":
        raise ValueError("Native rollout supports Harbor-side Opik; realtime hook replay is not yet supported")
    from opik_trace_gate import opik_tracing_enabled
    tracing = opik_tracing_enabled()
    if tracing:
        from opik.integrations.harbor import track_harbor
        track_harbor(project_name=os.environ["OPIK_PROJECT_NAME"])
    try:
        asyncio.run(serve(template, Path(os.environ["RL_QUEUE_DIR"]), Path(os.environ["JOBS_ROOT"]),
                          int(os.environ["RL_MAX_CONCURRENT"])))
    finally:
        if tracing:
            import opik
            opik.flush_tracker()


if __name__ == "__main__":
    main()
