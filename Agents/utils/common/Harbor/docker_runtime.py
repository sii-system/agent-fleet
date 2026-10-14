"""Compatibility fixes for Harbor 0.18's Docker execution and keepalive."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

VERIFIER_PATH_PREPEND_ENV = "HARBOR_VERIFIER_PATH_PREPEND"
SWE_ATLAS_IMAGE = "ghcr.io/scaleapi/swe-atlas"
SWE_ATLAS_KEEPALIVE = Path(__file__).parent / "overlays/swe-atlas-keepalive.yaml"


def patch_docker_runtime_from_env() -> bool:
    """Preserve verifier PATH and adapt Atlas images to Harbor's keepalive.

    TODO: Remove these patches when Harbor supports verifier PATH prepending
    and handles shell-only image entrypoints with its default startup command.
    """
    if os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower() != "docker":
        return False

    try:
        from harbor.environments.docker.docker import DockerEnvironment
    except ImportError:
        return False

    if getattr(DockerEnvironment, "_fleet_docker_runtime_patch_applied", False):
        return False

    original_exec = DockerEnvironment.exec
    original_compose_paths = DockerEnvironment._docker_compose_paths.fget

    async def exec_with_verifier_path(
        self, command: str, cwd=None, env=None, timeout_sec=None, user=None,
    ):
        if env and VERIFIER_PATH_PREPEND_ENV in env:
            env = dict(env)
            prefix = env.pop(VERIFIER_PATH_PREPEND_ENV)
            if prefix:
                # Expand inside the container, where PATH belongs to the image
                # or an explicit task override. Environment values are literal.
                command = f'export PATH={shlex.quote(prefix)}:"$PATH";\n{command}'
        return await original_exec(
            self, command=command, cwd=cwd, env=env,
            timeout_sec=timeout_sec, user=user,
        )

    def compose_paths_with_atlas_keepalive(self):
        paths = original_compose_paths(self)
        image = self.task_env_config.docker_image or ""
        repository = image.partition("@")[0].partition(":")[0]
        if not self._is_windows_container and repository == SWE_ATLAS_IMAGE:
            base = (
                self._DOCKER_COMPOSE_PREBUILT_PATH
                if self._use_prebuilt else self._DOCKER_COMPOSE_BUILD_PATH
            )
            # Task and caller overlays still follow this compatibility default
            # and can provide their own entrypoint or command.
            paths.insert(paths.index(base) + 1, SWE_ATLAS_KEEPALIVE)
        return paths

    DockerEnvironment.exec = exec_with_verifier_path
    DockerEnvironment._docker_compose_paths = property(compose_paths_with_atlas_keepalive)
    DockerEnvironment._fleet_docker_runtime_patch_applied = True
    return True
