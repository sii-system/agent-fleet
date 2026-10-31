"""DSec Harbor BaseEnvironment implementation.

The adapter consumes only the public Controller API. It supports Linux, one
container, root identity, and immutable OCI digests without exposing Compose,
mounts, GPUs, delivery policy, or Node internals.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)

from .client import DsecControllerClient
from .execution import DsecExecutionMixin
from .lifecycle import DsecLifecycleMixin
from .transfer import DsecTransferMixin

_DIGEST_REFERENCE = re.compile(
    r"^[^/@\s]+(?:/[^/@\s]+)+@sha256:[0-9a-f]{64}$"
)
_HARBOR_MANAGED_WRITABLE_TARGETS = {
    "/logs/agent",
    "/logs/artifacts",
    "/logs/verifier",
}


def _positive_int(value: str | int, name: str, *, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1 or (maximum is not None and parsed > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be positive{suffix}")
    return parsed


def _bool_setting(value: str | bool, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ValueError(f"{name} must be a boolean")


class DsecSandboxEnvironment(
    DsecLifecycleMixin,
    DsecTransferMixin,
    DsecExecutionMixin,
    BaseEnvironment,
):
    """Map one Harbor trial to one DSec Sandbox."""

    def __init__(
        self,
        *args: Any,
        image_ref: str | None = None,
        bundle_manifest_path: str | None = None,
        endpoint: str | None = None,
        ttl_seconds: int | str | None = None,
        ready_timeout_sec: int | str | None = None,
        close_timeout_sec: int | str | None = None,
        request_timeout_sec: int | str | None = None,
        exec_output_limit_bytes: int | str | None = None,
        default_exec_timeout_sec: int | str | None = None,
        transfer_max_bytes: int | str | None = None,
        transfer_timeout_sec: int | str | None = None,
        poll_interval_sec: float = 0.2,
        request_attempts: int | str = 3,
        **kwargs: Any,
    ) -> None:
        self._image_ref_override = (image_ref or "").strip()
        self._bundle_manifest_path = (
            bundle_manifest_path
            or os.environ.get("HARBOR_OPENSANDBOX_BUNDLE_MANIFEST", "")
        ).strip()
        controller_endpoint = (
            endpoint or os.environ.get("DSEC_SANDBOX_CONTROLLER_ENDPOINT", "")
        ).strip()
        if not controller_endpoint:
            raise ValueError("DSEC_SANDBOX_CONTROLLER_ENDPOINT is required")
        allow_insecure = _bool_setting(
            os.environ.get("DSEC_SANDBOX_ALLOW_INSECURE_HTTP", "0"),
            "DSEC_SANDBOX_ALLOW_INSECURE_HTTP",
        )
        self._ttl_seconds = _positive_int(
            ttl_seconds
            or os.environ.get("DSEC_SANDBOX_TTL_SECONDS", "7200"),
            "ttl_seconds",
        )
        self._ready_timeout_sec = _positive_int(
            ready_timeout_sec
            or os.environ.get("DSEC_SANDBOX_READY_TIMEOUT_SEC", "300"),
            "ready_timeout_sec",
        )
        self._close_timeout_sec = _positive_int(
            close_timeout_sec
            or os.environ.get("DSEC_SANDBOX_CLOSE_TIMEOUT_SEC", "120"),
            "close_timeout_sec",
        )
        request_timeout = _positive_int(
            request_timeout_sec
            or os.environ.get("DSEC_SANDBOX_REQUEST_TIMEOUT_SEC", "15"),
            "request_timeout_sec",
            maximum=60,
        )
        self._exec_output_limit_bytes = _positive_int(
            exec_output_limit_bytes
            or os.environ.get("DSEC_SANDBOX_EXEC_OUTPUT_LIMIT_BYTES", str(16 << 20)),
            "exec_output_limit_bytes",
        )
        self._default_exec_timeout_sec = _positive_int(
            default_exec_timeout_sec
            or os.environ.get("DSEC_SANDBOX_EXEC_TIMEOUT_SEC", "3600"),
            "default_exec_timeout_sec",
        )
        self._transfer_max_bytes = _positive_int(
            transfer_max_bytes
            or os.environ.get(
                "DSEC_SANDBOX_TRANSFER_MAX_BYTES", str(512 << 20)
            ),
            "transfer_max_bytes",
        )
        self._transfer_timeout_sec = _positive_int(
            transfer_timeout_sec
            or os.environ.get("DSEC_SANDBOX_TRANSFER_TIMEOUT_SEC", "1800"),
            "transfer_timeout_sec",
        )
        self._request_attempts = _positive_int(
            request_attempts, "request_attempts", maximum=5
        )
        if not 0.05 <= poll_interval_sec <= 10:
            raise ValueError("poll_interval_sec must be between 0.05 and 10")
        self._poll_interval_sec = poll_interval_sec
        self._client = DsecControllerClient(
            controller_endpoint,
            allow_insecure_http=allow_insecure,
            request_timeout_sec=request_timeout,
        )
        self._sandbox_id = ""
        self._execution_sequence = 0
        super().__init__(*args, **kwargs)
        self.default_user = "root"
        self._network_mode = "bridge" if self._network_is_public else "none"

    @staticmethod
    def type() -> str:
        return "dsec"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            stream=False,
            gpus=False,
            tpus=False,
            disable_internet=True,
            # Temporary compatibility: Harbor may request an allowlist for an
            # agent phase while the Sandbox remains on its public bridge. The
            # transition emits a warning in _apply_network_policy.
            # TODO(network-policy): implement Node-enforced egress rules in a
            # dedicated follow-up PR, then remove this compatibility path.
            network_allowlist=True,
            network_allowlist_hostnames=True,
            network_allowlist_wildcard_hostnames=True,
            network_allowlist_ipv4_addresses=True,
            network_allowlist_ipv6_addresses=True,
            network_allowlist_ipv4_cidrs=True,
            network_allowlist_ipv6_cidrs=True,
            dynamic_network_policy=True,
            windows=False,
            mounted=False,
            docker_compose=False,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # Harbor must accept ordinary task definitions during initial integration,
        # even though DSec intentionally ignores their per-Sandbox quotas.
        return EnvironmentResourceCapabilities(
            cpu_limit=True,
            memory_limit=True,
            fractional_cpus=True,
        )

    @classmethod
    def preflight(cls) -> None:
        endpoint = os.environ.get("DSEC_SANDBOX_CONTROLLER_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("DSEC_SANDBOX_CONTROLLER_ENDPOINT is required")
        DsecControllerClient(
            endpoint,
            allow_insecure_http=_bool_setting(
                os.environ.get("DSEC_SANDBOX_ALLOW_INSECURE_HTTP", "0"),
                "DSEC_SANDBOX_ALLOW_INSECURE_HTTP",
            ),
        )

    def _bundle_image_ref(self) -> str:
        if not self._bundle_manifest_path:
            return ""
        path = Path(self._bundle_manifest_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            version = data["schema_version"]
            main = data["main_service"] if version == 1 else data["main"]
            image = data["services"][main]["image"]
            return str(
                image["sandbox_ref"] if version == 1 else image["digest_ref"]
            ).strip()
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid DSec Bundle Manifest") from error

    @property
    def _image_ref(self) -> str:
        return (
            self._image_ref_override
            or self._bundle_image_ref()
            or str(self.task_env_config.docker_image or "").strip()
        )

    def _validate_definition(self) -> None:
        compose_paths = (
            self.environment_dir / "docker-compose.yaml",
            self.environment_dir / "docker-compose.yml",
        )
        if any(path.exists() for path in compose_paths):
            raise ValueError("DSec backend does not support Docker Compose")
        if not _DIGEST_REFERENCE.fullmatch(self._image_ref):
            raise ValueError("DSec requires a canonical OCI manifest digest reference")
        if any(
            value is not None
            for value in (
                self._effective_cpus,
                self._effective_memory_mb,
                self._effective_storage_mb,
            )
        ):
            self.logger.warning(
                "DSec does not enforce per-Sandbox resource limits yet; "
                "continuing with shared host scheduling"
            )
        # TODO: advertise and enforce task quotas only after DSec provides
        # per-Sandbox CPU, memory, PID, and storage accounting.
        for mount in getattr(self, "_mounts", []):
            if mount.get("type") != "bind":
                raise ValueError("DSec only accepts bind inputs")
            if mount.get("read_only"):
                continue
            if str(mount.get("target") or "") not in _HARBOR_MANAGED_WRITABLE_TARGETS:
                raise ValueError(
                    "DSec only accepts Harbor-managed writable output mounts"
                )

    def _resource_request(self) -> dict[str, int | str]:
        """Select deployment-owned shared scheduling without task quotas."""
        return {
            "policy": "shared/v1",
            "cpu_millicores": 0,
            "memory_bytes": 0,
            "pids_limit": 0,
        }

    async def _apply_network_policy(self, network_policy: Any) -> None:
        """Warn when Harbor requests an allowlist that DSec cannot enforce."""

        mode = getattr(network_policy.network_mode, "value", network_policy.network_mode)
        if mode == "allowlist":
            self.logger.warning(
                "DSec does not enforce Harbor network allowlists yet; "
                "continuing with the Sandbox public bridge. Enforcement is "
                "deferred to a dedicated follow-up PR"
            )
