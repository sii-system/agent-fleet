"""Harbor E2B environment for host-configured prebuilt templates."""

from __future__ import annotations

import os
import shlex

from e2b import AsyncSandbox
from e2b.connection_config import ConnectionConfig
from harbor.environments.e2b import E2BEnvironment
from harbor.models.task.config import NetworkMode

_TRUE_VALUES = {"1", "true", "yes", "on"}
_HARBOR_E2B_DEFAULT_SANDBOX_TIMEOUT_SEC = 86_400


def _patch_http_sandbox_url_from_env() -> None:
    """Support E2B-compatible deployments that expose envd over plain HTTP."""

    if os.environ.get("E2B_FORCE_HTTP", "").strip().lower() not in _TRUE_VALUES:
        return
    if getattr(ConnectionConfig, "_fleet_http_sandbox_url_patch_applied", False):
        return

    def get_sandbox_url(self, sandbox_id, sandbox_domain):  # type: ignore[no-untyped-def]
        return f"http://{self.get_host(sandbox_id, sandbox_domain, self.envd_port)}"

    ConnectionConfig.get_sandbox_url = get_sandbox_url
    ConnectionConfig._fleet_http_sandbox_url_patch_applied = True


_patch_http_sandbox_url_from_env()


def _prebuilt_template() -> str:
    template = os.environ.get("HARBOR_E2B_PREBUILT_TEMPLATE", "").strip()
    if not template:
        template = os.environ.get("E2B_TEMPLATE", "").strip()
    if not template:
        raise RuntimeError(
            "HARBOR_E2B_PREBUILT_TEMPLATE is required for the prebuilt E2B environment"
        )
    return template


def _sandbox_timeout_sec() -> int:
    raw_timeout = (
        os.environ.get("HARBOR_E2B_SANDBOX_TIMEOUT_SEC", "").strip()
        or os.environ.get("E2B_SANDBOX_TIMEOUT_SEC", "").strip()
        or str(_HARBOR_E2B_DEFAULT_SANDBOX_TIMEOUT_SEC)
    )
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise RuntimeError("E2B sandbox timeout must be an integer") from exc
    if timeout <= 0:
        raise RuntimeError("E2B sandbox timeout must be positive")
    return timeout


class PrebuiltE2BEnvironment(E2BEnvironment):
    """Run a Harbor task in one explicitly configured prebuilt E2B template.

    This compatibility mode deliberately skips task Dockerfile conversion and
    template building. It is useful while an E2B-compatible deployment exposes
    Sandbox lifecycle APIs but not a working dynamic template runtime.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._template_name = _prebuilt_template()

    async def start(self, force_build: bool) -> None:
        if force_build:
            raise RuntimeError("force_build is not supported by the prebuilt E2B environment")
        await super().start(force_build=False)

    async def _does_template_exist(self) -> bool:
        # The deployment supplies an opaque template ID rather than a Harbor
        # alias, so no alias/tag lookup is valid on this path.
        return True

    async def _create_sandbox(self) -> None:
        self._sandbox = await AsyncSandbox.create(
            template=self._template_name,
            timeout=_sandbox_timeout_sec(),
            allow_internet_access=(
                self.network_policy.network_mode != NetworkMode.NO_NETWORK
            ),
            network=self._sandbox_create_network_options(),
        )
        workdir = shlex.quote(str(self._workdir))
        result = await self._sandbox.commands.run(
            f"mkdir -p -- {workdir} && chmod 0777 -- {workdir}",
            user="root",
            cwd="/",
            timeout=30,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"failed to prepare task workdir {self._workdir}: "
                f"exit {result.exit_code}: {result.stderr or result.stdout}"
            )
