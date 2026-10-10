"""Select the existing SBX/Harbor Docker backend for an ALE Linux trial."""

import json
import os

from harbor.environments.docker.docker import DockerEnvironment

DEFAULT_IMAGE = "agentslastexam/ale-ubuntu22-docker:latest"


def sbx_configured(profile):
    key = os.environ.get("QZ_SANDBOX_API_KEY", "").strip() or os.environ.get("SBX_API_KEY", "").strip()
    if not key:
        key = os.environ.get("E2B_API_KEY", "").strip().startswith("sbx_")
    template = profile.get("sbx_template") or os.environ.get("QZ_SANDBOX_TEMPLATE") or os.environ.get("QZ_SANDBOX_TEMPLATE_MAP")
    return bool(key and template)


def create_backend(environment, profile, mode, kwargs):
    if mode == "auto":
        mode = "sbx" if sbx_configured(profile) else "docker"
    directory = environment.trial_paths.trial_dir / "ale-linux-environment"
    directory.mkdir(parents=True, exist_ok=True)
    image = profile.get("docker_image", DEFAULT_IMAGE)
    (directory / "Dockerfile").write_text(f"FROM {image}\nWORKDIR /workspace\n")
    config = environment.task_env_config.model_copy(deep=True)
    options = {**kwargs, "environment_dir": directory, "environment_name": environment.environment_name,
               "session_id": environment.session_id, "trial_paths": environment.trial_paths, "task_env_config": config}
    if mode == "sbx":
        if not sbx_configured(profile):
            raise ValueError("SBX requires a sandbox key and a prepared ALE template")
        from qz_e2b_sandbox import QzSandboxEnvironment
        if profile.get("sbx_template"):
            options["template"] = profile["sbx_template"]
        return mode, QzSandboxEnvironment(**options)
    config.docker_image = image
    # Enable only when the operator supplies a prepared image/runtime for nested tasks.
    nested = profile.get("docker", {})
    compose = {"services": {"main": {"entrypoint": ["/dockerstartup/entrypoint.sh"],
        "command": ["--wait"], "shm_size": nested.get("shm_size", "2g"),
        "privileged": nested.get("privileged", False),
        "environment": {"ALE_ENABLE_DIND": "1" if nested.get("enable_dind", False) else "0"}}}}
    (directory / "docker-compose.yaml").write_text(json.dumps(compose))
    return mode, DockerEnvironment(**options)


async def cleanup_sbx_attempt(backend):
    """Require successful deletion before provisioning a second environment."""
    if getattr(backend, "_sandbox", None) is not None:
        # E2B's public stop() logs and suppresses deletion failures. Keep its
        # handle available on failure and let Harbor report the cleanup error.
        await backend._stop_sandbox()
        backend._sandbox = None
    else:
        await backend.stop(delete=True)
