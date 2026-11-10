"""YiCloud adapter: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import os
from typing import Any

from harbor.environments.base import BaseEnvironment
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)

from .client import YiCloudClientMixin, _require_yicloud_sdk, _yicloud_image_ref
from .config import (
    _bool_setting,
    _positive_int,
    _required_env,
    _required_registry_host,
    _validate_managed_image_ref,
)
from .execution import YiCloudExecutionMixin
from .lifecycle import YiCloudLifecycleMixin
from .s3_transfer import S3UploadStore
from .services import ServiceRuntime, YiCloudServicesMixin
from .transfer import YiCloudTransferMixin


class YiCloudOpenSandboxEnvironment(
    YiCloudLifecycleMixin,
    YiCloudServicesMixin,
    YiCloudTransferMixin,
    YiCloudExecutionMixin,
    YiCloudClientMixin,
    BaseEnvironment,
):
    """Harbor adapter composing the existing YiCloud runtime responsibilities."""

    def __init__(
        self,
        *args,
        image_ref: str | None = None,
        bundle_manifest_path: str | None = None,
        project_name: str | None = None,
        lifecycle_minutes: int | str | None = None,
        ready_timeout_sec: int | None = None,
        request_timeout_sec: int = 180,
        cleanup_wait_sec: int = 45,
        status_log_interval_sec: int | None = None,
        retain_on_start_failure: bool | str | None = None,
        retain_after_trial: bool | str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        **kwargs,
    ) -> None:
        self._image_ref_override = (image_ref or "").strip()
        self._project_name = (
            project_name or os.environ.get("YICLOUD_PROJECT_NAME", "")
        ).strip()
        self._environment_id = os.environ.get(
            "YICLOUD_SANDBOX_ENVIRONMENT_ID", ""
        ).strip()
        self._environment_name = os.environ.get(
            "YICLOUD_SANDBOX_ENVIRONMENT_NAME", ""
        ).strip()
        self._lifecycle_minutes = _positive_int(
            lifecycle_minutes
            if lifecycle_minutes is not None
            else os.environ.get("YICLOUD_SANDBOX_LIFECYCLE_MINUTES", "120"),
            "lifecycle_minutes",
        )
        self._ready_timeout_sec = _positive_int(
            ready_timeout_sec
            if ready_timeout_sec is not None
            else os.environ.get("YICLOUD_SANDBOX_READY_TIMEOUT_SEC", "300"),
            "ready_timeout_sec",
        )
        self._request_timeout_sec = request_timeout_sec
        self._cleanup_wait_sec = cleanup_wait_sec
        self._status_log_interval_sec = _positive_int(
            status_log_interval_sec
            if status_log_interval_sec is not None
            else os.environ.get("YICLOUD_SANDBOX_STATUS_LOG_INTERVAL_SEC", "30"),
            "status_log_interval_sec",
        )
        self._retain_on_start_failure = _bool_setting(
            retain_on_start_failure
            if retain_on_start_failure is not None
            else os.environ.get("YICLOUD_SANDBOX_RETAIN_ON_START_FAILURE", "0"),
            "retain_on_start_failure",
        )
        self._retain_after_trial = _bool_setting(
            retain_after_trial
            if retain_after_trial is not None
            else os.environ.get("YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL", "0"),
            "retain_after_trial",
        )
        self._request_cpu = (cpu or os.environ.get("YICLOUD_SANDBOX_CPU", "2")).strip()
        self._request_memory = (
            memory or os.environ.get("YICLOUD_SANDBOX_MEMORY", "8Gi")
        ).strip()
        if not self._request_cpu:
            raise ValueError("cpu must not be empty")
        if not self._request_memory:
            raise ValueError("memory must not be empty")
        self._upload_backend = (
            os.environ.get("YICLOUD_SANDBOX_UPLOAD_BACKEND", "http").strip().lower()
        )
        if self._upload_backend not in {"http", "s3", "auto"}:
            raise ValueError("YICLOUD_SANDBOX_UPLOAD_BACKEND must be http, s3, or auto")
        self._oracle_solution_upload_backend = os.environ.get(
            "YICLOUD_SANDBOX_ORACLE_SOLUTION_UPLOAD_BACKEND",
            "inherit",
        ).strip().lower()
        if self._oracle_solution_upload_backend not in {
            "inherit",
            "execd",
            "inline",
        }:
            raise ValueError(
                "YICLOUD_SANDBOX_ORACLE_SOLUTION_UPLOAD_BACKEND must be "
                "inherit, execd, or inline"
            )
        self._oracle_skip_agent_log_reupload = _bool_setting(
            os.environ.get(
                "YICLOUD_SANDBOX_ORACLE_SKIP_AGENT_LOG_REUPLOAD",
                "0",
            ),
            "YICLOUD_SANDBOX_ORACLE_SKIP_AGENT_LOG_REUPLOAD",
        )
        self._oracle_skip_agent_log_transfer = _bool_setting(
            os.environ.get(
                "YICLOUD_SANDBOX_ORACLE_SKIP_AGENT_LOG_TRANSFER",
                "0",
            ),
            "YICLOUD_SANDBOX_ORACLE_SKIP_AGENT_LOG_TRANSFER",
        )
        self._s3_upload_store: S3UploadStore | None = None
        self._s3_upload_setup_error: Exception | None = None
        if self._upload_backend in {"s3", "auto"}:
            try:
                self._s3_upload_store = S3UploadStore.from_environment()
            except ValueError as exc:
                if self._upload_backend == "s3":
                    raise
                # Preserve auto's HTTP fallback even when S3 configuration
                # cannot be initialized. The first actual upload logs the
                # same structured fallback warning as runtime S3 failures.
                self._s3_upload_setup_error = exc
        self._base_client: Any | None = None
        self._sandbox_service: Any | None = None
        self._sandbox_id = ""
        self._sandbox_name = ""
        self._command_url = ""
        self._access_token = ""
        self._bundle_manifest_path = (
            bundle_manifest_path
            or os.environ.get("HARBOR_OPENSANDBOX_BUNDLE_MANIFEST", "")
        ).strip()
        self._bundle: dict[str, Any] | None = None
        self._services: dict[str, ServiceRuntime] = {}
        self._main_service = "main"
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "opensandbox"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            gpus=False, disable_internet=False, mounted=False
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @classmethod
    def preflight(cls) -> None:
        _require_yicloud_sdk()
        for name in (
            "YICLOUD_PUBLIC_KEY",
            "YICLOUD_SECRET_KEY",
            "YICLOUD_PROJECT_NAME",
            "YICLOUD_HARBOR_HOST",
        ):
            _required_env(name)
        _required_registry_host()
        if not (
            os.environ.get("YICLOUD_SANDBOX_ENVIRONMENT_ID", "").strip()
            or os.environ.get("YICLOUD_SANDBOX_ENVIRONMENT_NAME", "").strip()
        ):
            raise RuntimeError(
                "YICLOUD_SANDBOX_ENVIRONMENT_ID or "
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME is required"
            )

    @property
    def _image_source_ref(self) -> str:
        if self._bundle is not None:
            main = self._services.get(self._main_service)
            if main is not None:
                image = main.spec.get("image") or {}
                return str(
                    image.get("digest_ref") or image.get("sandbox_ref") or ""
                ).strip()
        return (
            self._image_ref_override
            or str(self.task_env_config.docker_image or "").strip()
        )

    @property
    def _image_ref(self) -> str:
        """YiCloud's normalized image-ref value used for binding validation."""
        return _yicloud_image_ref(self._image_source_ref)

    def _validate_definition(self) -> None:
        self._load_bundle()
        if self._bundle is not None:
            self._capability_gate()
            return
        compose_paths = (
            self.environment_dir / "docker-compose.yaml",
            self.environment_dir / "docker-compose.yml",
        )
        if any(path.exists() for path in compose_paths):
            raise ValueError(
                "YiCloud OpenSandbox does not support docker-compose tasks"
            )
        if not self._image_ref:
            raise ValueError(
                "YiCloud OpenSandbox requires a prebuilt image via "
                "task.environment.docker_image or environment kwarg image_ref"
            )
        _validate_managed_image_ref(
            self._image_source_ref,
            "YiCloud OpenSandbox image",
        )
