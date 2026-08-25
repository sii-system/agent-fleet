"""YiCloud provider adapter for Harbor's OpenSandbox environment contract.

Harbor 0.18 ships an OpenSandbox backend for the upstream ``opensandbox`` SDK.
YiCloud exposes compatible sandbox execution semantics behind its own OpenAPI
control plane and OGW request signing, so Agent Fleet loads this class through
Harbor's custom environment import-path support instead of patching Harbor.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from opensandbox_s3_upload import S3UploadArtifact, S3UploadStore

EXPECTED_YICLOUD_SDK_VERSION = "0.3.1"
EXIT_MARKER = "__HARBOR_YICLOUD_OPENSANDBOX_EXIT_CODE__="
TERMINAL_FAILURE_STATES = {
    "failed",
    "error",
    "stopped",
    "terminated",
    "killed",
    "expired",
}
HOSTS_BLOCK_BEGIN = "# HARBOR COMPOSE BEGIN"
HOSTS_BLOCK_END = "# HARBOR COMPOSE END"
COMPOSE_START_MARKER_PREFIX = "/tmp/.harbor-compose-start-"
# The YiCloud gateway currently rejects multipart bodies around 1 MiB and
# above. Base64 expands each chunk by 4/3, so 512 KiB leaves enough room for
# multipart metadata while avoiding thousands of tiny requests.
UPLOAD_CHUNK_BYTES = 512 * 1024
FAST_UPLOAD_CHUNK_BYTES = 512 * 1024
DEFAULT_EXECD_REQUEST_ATTEMPTS = 4
SANDBOX_STATUS_REQUEST_ATTEMPTS = 5
CONTROL_PLANE_AUTH_ATTEMPTS = 5
EXECD_DETACHED_COMMAND_MIN_TIMEOUT_SEC = 300
EXECD_DETACHED_POLL_INTERVAL_SEC = 5
DETACHED_PENDING_MARKER = "__HARBOR_YICLOUD_DETACHED_PENDING__"
S3_HTTP_BOOTSTRAP_PATH = "/tmp/.harbor-s3-http-get-v1.sh"
# Current YiCloud task images are Linux/amd64 glibc images and normally carry
# curl, wget, or python3. A few minimal images only carry Bash. For those
# images, upload this small downloader once per Sandbox instead of uploading a
# Python/Node runtime or baking an agent-specific tool into every task image.
S3_HTTP_BOOTSTRAP = r"""#!/usr/bin/env bash
set -euo pipefail

url=${HARBOR_S3_URL:?HARBOR_S3_URL is required}
output=${1:?output path is required}
case "$url" in
  http://*) rest=${url#http://} ;;
  *) echo "minimal S3 downloader only supports plain HTTP URLs" >&2; exit 64 ;;
esac
authority=${rest%%/*}
if [[ "$rest" == */* ]]; then
  request_path=/${rest#*/}
else
  request_path=/
fi
if [[ "$authority" == *:* ]]; then
  host=${authority%%:*}
  port=${authority##*:}
else
  host=$authority
  port=80
fi
[[ -n "$host" && -n "$port" ]] || {
  echo "invalid S3 download URL" >&2
  exit 64
}

exec 3<>"/dev/tcp/$host/$port"
printf 'GET %s HTTP/1.1\r\nHost: %s\r\nAccept: */*\r\nConnection: close\r\n\r\n' \
  "$request_path" "$authority" >&3
IFS= read -r status <&3
status=${status%$'\r'}
[[ "$status" == HTTP/1.0\ 200\ * || "$status" == HTTP/1.1\ 200\ * ]] || {
  echo "S3 download failed: $status" >&2
  exit 65
}
while IFS= read -r header <&3; do
  header=${header%$'\r'}
  [[ -z "$header" ]] && break
  [[ "${header,,}" != transfer-encoding:*chunked* ]] || {
    echo "chunked S3 response is unsupported by minimal downloader" >&2
    exit 66
  }
done
cat <&3 >"$output"
"""


class YiCloudSandboxReadyTimeoutError(RuntimeError):
    """The YiCloud control plane did not schedule a Sandbox before our deadline."""


def _execd_request_attempts() -> int:
    return _positive_int(
        os.environ.get(
            "YICLOUD_EXECD_REQUEST_ATTEMPTS",
            str(DEFAULT_EXECD_REQUEST_ATTEMPTS),
        ),
        "YICLOUD_EXECD_REQUEST_ATTEMPTS",
    )


def _retryable_execd_error(error: requests.RequestException) -> bool:
    if isinstance(error, requests.HTTPError):
        return (
            error.response is not None
            and error.response.status_code in {502, 503, 504}
        )
    return isinstance(
        error,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


class ServiceRuntime:
    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.sandbox_id = ""
        self.sandbox_name = ""
        self.command_url = ""
        self.access_token = ""
        self.internal_address = ""
        self.state = "CREATING"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is unset: {name}")
    return value


def _require_yicloud_sdk() -> None:
    try:
        installed = version("yicloud-sdk-python")
    except PackageNotFoundError as exc:
        raise RuntimeError("yicloud-sdk-python is not installed") from exc
    if installed != EXPECTED_YICLOUD_SDK_VERSION:
        raise RuntimeError(
            f"expected yicloud-sdk-python=={EXPECTED_YICLOUD_SDK_VERSION}, "
            f"found {installed}"
        )


def _bypass_local_proxy_for_api_host() -> None:
    api_host = os.environ.get("YICLOUD_API_HOST", "https://gate.yicloud.com.cn")
    hostname = urlsplit(api_host).hostname
    if not hostname:
        raise ValueError(f"unable to parse YICLOUD_API_HOST hostname: {api_host!r}")
    for key in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in os.environ.get(key, "").split(",")]
        if hostname not in entries:
            os.environ[key] = ",".join(item for item in [*entries, hostname] if item)


def _state_of(data: Any) -> str:
    status = getattr(data, "Status", None)
    state = getattr(status, "State", "") or getattr(data, "RunState", "")
    # yicloud-sdk-python 0.3.1 declares lowercase state values, while older
    # gateways returned title-case values. Normalize both representations so a
    # running sandbox is not mistaken for a pending one until ready timeout.
    return str(state or "").strip().lower()


def _status_reason_of(data: Any) -> str:
    status = getattr(data, "Status", None)
    return str(getattr(status, "Reason", "") or "").strip()


def _yicloud_image_ref(ref: str) -> str:
    """Translate a full OCI reference to YiCloud's ``Image.Ref`` format.

    The Bundle retains the complete OCI digest reference used for Registry
    publication and verification.  YiCloud's CreateSandbox SDK explicitly
    requires ``Image.Ref`` without a registry host, so this provider boundary
    keeps the immutable repository-and-digest portion only.
    """
    value = str(ref or "").strip()
    repository, marker, digest = value.partition("@")
    first, separator, remainder = repository.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        return f"{remainder}{marker}{digest}" if marker else remainder
    return value


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return parsed


def _bool_setting(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        f"{name} must be one of 1/0, true/false, yes/no, or on/off; "
        f"got {value!r}"
    )


def _access_token_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    return str(
        getattr(data, "AccessToken", "")
        or getattr(endpoints, "AccessToken", "")
        or ""
    )


def _image_ref_of(data: Any) -> str:
    image = getattr(data, "Image", None)
    return str(
        getattr(image, "Ref", "") or getattr(image, "Uri", "") or ""
    ).strip()


def _image_ref_aliases(image_ref: str) -> set[str]:
    value = str(image_ref or "").strip()
    if not value:
        return set()
    aliases = {value}
    if "://" in value:
        path = urlsplit(value).path.lstrip("/")
        if path:
            aliases.add(path)
    else:
        normalized = _yicloud_image_ref(value)
        if normalized != value:
            aliases.add(normalized)
    return aliases


def _is_external_image_ref(image_ref: str) -> bool:
    value = str(image_ref or "").strip()
    return "://" in value or _yicloud_image_ref(value) != value


def _host_routable_proxy_url(proxy_url: str) -> str:
    origin = os.environ.get("YICLOUD_SANDBOX_PROXY_ORIGIN", "").strip()
    if not origin:
        return proxy_url
    source = urlsplit(proxy_url)
    target = urlsplit(origin)
    if target.scheme not in {"http", "https"} or not target.netloc:
        raise ValueError(
            "YICLOUD_SANDBOX_PROXY_ORIGIN must be an HTTP(S) origin"
        )
    path = f"{target.path.rstrip('/')}/{source.path.lstrip('/')}"
    return urlunsplit((target.scheme, target.netloc, path, source.query, ""))


def _environment_id_by_exact_name(
    sandbox: Any, project_name: str, environment_name: str
) -> str:
    listed = sandbox.list_sandbox_environments(
        None,
        sandbox.models.ListSandboxEnvironmentsReq(
            ProjectName=project_name,
            Keyword=environment_name,
            Limit=100,
            Offset=0,
        ),
    )
    matches = [
        item
        for item in (getattr(listed, "Items", None) or [])
        if str(getattr(item, "Name", "") or "").strip() == environment_name
    ]
    if not matches:
        raise RuntimeError(
            f"YiCloud Sandbox environment not found by exact name: "
            f"{environment_name!r}"
        )
    if len(matches) != 1:
        ids = sorted(str(getattr(item, "Id", "") or "") for item in matches)
        raise RuntimeError(
            f"YiCloud Sandbox environment name is ambiguous: "
            f"{environment_name!r}; ids={ids}"
        )
    environment_id = str(getattr(matches[0], "Id", "") or "").strip()
    if not environment_id:
        raise RuntimeError(
            f"YiCloud Sandbox environment has no ID: {environment_name!r}"
        )
    return environment_id


def _validate_sandbox_binding(
    data: Any, expected_environment_id: str, expected_image_ref: str
) -> None:
    actual_environment_id = str(
        getattr(data, "EnvironmentId", "") or ""
    ).strip()
    actual_image_ref = _image_ref_of(data)
    if actual_environment_id != expected_environment_id:
        raise RuntimeError(
            "YiCloud Sandbox environment binding mismatch: "
            f"expected={expected_environment_id!r}, "
            f"actual={actual_environment_id!r}"
        )
    aliases_match = _image_ref_aliases(actual_image_ref).intersection(
        _image_ref_aliases(expected_image_ref)
    )
    registry_mismatch = (
        actual_image_ref != expected_image_ref
        and _is_external_image_ref(actual_image_ref)
        and _is_external_image_ref(expected_image_ref)
    )
    if registry_mismatch or not aliases_match:
        raise RuntimeError(
            "YiCloud Sandbox image binding mismatch: "
            f"expected={expected_image_ref!r}, actual={actual_image_ref!r}"
        )


def _command_url_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    for endpoint in (getattr(endpoints, "Endpoints", None) or {}).values():
        proxy_url = str(getattr(endpoint, "ProxyUrl", "") or "")
        if not proxy_url.startswith(("http://", "https://")):
            continue
        proxy_url = _host_routable_proxy_url(proxy_url)
        parsed = urlsplit(proxy_url)
        path = parsed.path.rstrip("/")
        path = path.removesuffix("/ping")
        path += "/command"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    raise RuntimeError("running YiCloud Sandbox returned no execd proxy endpoint")


def _internal_address_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    for endpoint in (getattr(endpoints, "Endpoints", None) or {}).values():
        value = str(getattr(endpoint, "InternalUrl", "") or "")
        if not value:
            continue
        parsed = urlsplit(value if "://" in value else f"tcp://{value}")
        if parsed.hostname:
            return parsed.hostname
    raise RuntimeError("running YiCloud Sandbox returned no internal endpoint address")


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            events.append({"type": "unparsed", "text": line})
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


class YiCloudOpenSandboxEnvironment(BaseEnvironment):
    """Run one Harbor task in a YiCloud-managed OpenSandbox instance."""

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
            else os.environ.get(
                "YICLOUD_SANDBOX_STATUS_LOG_INTERVAL_SEC", "30"
            ),
            "status_log_interval_sec",
        )
        self._retain_on_start_failure = _bool_setting(
            retain_on_start_failure
            if retain_on_start_failure is not None
            else os.environ.get(
                "YICLOUD_SANDBOX_RETAIN_ON_START_FAILURE", "0"
            ),
            "retain_on_start_failure",
        )
        self._retain_after_trial = _bool_setting(
            retain_after_trial
            if retain_after_trial is not None
            else os.environ.get(
                "YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL", "0"
            ),
            "retain_after_trial",
        )
        self._request_cpu = (
            cpu or os.environ.get("YICLOUD_SANDBOX_CPU", "2")
        ).strip()
        self._request_memory = (
            memory or os.environ.get("YICLOUD_SANDBOX_MEMORY", "8Gi")
        ).strip()
        if not self._request_cpu:
            raise ValueError("cpu must not be empty")
        if not self._request_memory:
            raise ValueError("memory must not be empty")
        self._upload_backend = os.environ.get(
            "YICLOUD_SANDBOX_UPLOAD_BACKEND", "http"
        ).strip().lower()
        if self._upload_backend not in {"http", "s3"}:
            raise ValueError(
                "YICLOUD_SANDBOX_UPLOAD_BACKEND must be http or s3"
            )
        self._s3_download_timeout_sec = _positive_int(
            os.environ.get(
                "YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC", "1800"
            ),
            "YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC",
        )
        self._s3_upload_store = (
            S3UploadStore.from_environment()
            if self._upload_backend == "s3"
            else None
        )
        self._s3_downloader_ready = False
        self._s3_downloader_lock: asyncio.Lock | None = None
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
        return EnvironmentCapabilities(gpus=False, disable_internet=False, mounted=False)

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
        ):
            _required_env(name)
        if not (
            os.environ.get("YICLOUD_SANDBOX_ENVIRONMENT_ID", "").strip()
            or os.environ.get(
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME", ""
            ).strip()
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
                return str(image.get("digest_ref") or image.get("sandbox_ref") or "").strip()
        return self._image_ref_override or str(
            self.task_env_config.docker_image or ""
        ).strip()

    @property
    def _image_ref(self) -> str:
        """YiCloud's normalized image-ref value used for binding validation."""
        return _yicloud_image_ref(self._image_source_ref)

    def _load_bundle(self) -> None:
        if self._bundle is not None:
            return
        if not self._bundle_manifest_path:
            return
        path = Path(self._bundle_manifest_path)
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot read OpenSandbox Bundle Manifest {path}: {exc}") from exc
        if not isinstance(bundle, dict):
            raise TypeError("OpenSandbox Bundle Manifest must be a JSON object")
        schema = bundle.get("schema_version")
        main = bundle.get("main") if schema == 2 else bundle.get("main_service")
        services = bundle.get("services")
        if schema not in {1, 2} or not isinstance(main, str) or not isinstance(services, dict) or main not in services:
            raise RuntimeError("OpenSandbox Bundle Manifest has an invalid schema or main service")
        resolved: dict[str, ServiceRuntime] = {}
        for name, spec in services.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise TypeError("OpenSandbox Bundle Manifest has an invalid service entry")
            image = spec.get("image") or {}
            ref = image.get("digest_ref") if schema == 2 else image.get("sandbox_ref")
            if not isinstance(ref, str) or not ref:
                raise RuntimeError(f"Bundle service {name!r} has no immutable image reference")
            resolved[name] = ServiceRuntime(name=name, spec=spec)
        self._bundle = bundle
        self._services = resolved
        self._main_service = main

    def _capability_gate(self) -> None:
        if self._bundle is None:
            return
        requirements = self._bundle.get("requirements") or {}
        unsupported = []
        mapping = {
            "shared_volumes": "shared_volumes",
            "fixed_ip": "fixed_ip",
            "multiple_networks": "multiple_networks",
            "net_admin": "net_admin",
            "sys_admin": "sys_admin",
            "privileged": "privileged",
        }
        for field, label in mapping.items():
            if requirements.get(field):
                unsupported.append(label)
        unsupported.extend(requirements.get("unsupported_features") or [])
        # TODO: Consider a routing environment that falls back to Docker when
        # OpenSandbox cannot satisfy a task's required capabilities.
        if requirements.get("multi_service"):
            for name, runtime in self._services.items():
                if name == self._main_service:
                    continue
                contract = runtime.spec.get("runtime")
                ports = contract.get("internal_ports") if isinstance(contract, dict) else None
                if not isinstance(ports, list) or not ports:
                    unsupported.append(f"service {name} lacks runtime.internal_ports")
        if unsupported:
            raise RuntimeError(
                "OpenSandbox capability gate rejected Bundle before create: "
                + ", ".join(sorted(str(item) for item in unsupported))
            )

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
            raise ValueError("YiCloud OpenSandbox does not support docker-compose tasks")
        if not self._image_ref:
            raise ValueError(
                "YiCloud OpenSandbox requires a prebuilt image via "
                "task.environment.docker_image or environment kwarg image_ref"
            )

    def _initialize_service(self) -> None:
        _require_yicloud_sdk()
        _bypass_local_proxy_for_api_host()
        if not self._project_name:
            raise RuntimeError("YICLOUD_PROJECT_NAME is unset")

        from yicloud import base
        from yicloud.services import sandbox

        self._base_client = base.new_client()
        sandbox.use_client(self._base_client)
        self._sandbox_service = sandbox

    def _resolve_environment_id(self) -> str:
        sandbox = self._sandbox_service
        if sandbox is None:
            raise RuntimeError("YiCloud Sandbox service is not initialized")
        if not self._environment_id and not self._environment_name:
            raise RuntimeError(
                "YiCloud Sandbox environment is unset; configure "
                "YICLOUD_SANDBOX_ENVIRONMENT_ID or "
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME"
            )
        if self._environment_name:
            resolved = _environment_id_by_exact_name(
                sandbox, self._project_name, self._environment_name
            )
            if self._environment_id and self._environment_id != resolved:
                raise RuntimeError(
                    "YiCloud Sandbox environment ID/name mismatch: "
                    f"id={self._environment_id!r}, "
                    f"name={self._environment_name!r}, "
                    f"resolved_id={resolved!r}"
                )
            self._environment_id = resolved
        else:
            environment = sandbox.get_sandbox_environment(
                None,
                sandbox.models.GetSandboxEnvironmentReq(
                    ProjectName=self._project_name,
                    EnvironmentId=self._environment_id,
                ),
            )
            actual_id = str(getattr(environment, "Id", "") or "").strip()
            if actual_id != self._environment_id:
                raise RuntimeError(
                    "YiCloud Sandbox environment lookup mismatch: "
                    f"expected={self._environment_id!r}, actual={actual_id!r}"
                )
            self._environment_name = str(
                getattr(environment, "Name", "") or ""
            ).strip()
        return self._environment_id

    def _make_sandbox_name(self) -> str:
        raw = f"harbor-{self.session_id}".lower()
        normalized = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
        return (normalized or "harbor-trial")[:63].rstrip("-")

    async def _retry_control_plane_auth(
        self,
        operation: str,
        function: Any,
        *args: Any,
    ) -> Any:
        for attempt in range(1, CONTROL_PLANE_AUTH_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(function, *args)
            except Exception as error:
                if (
                    getattr(error, "code", None) != 101
                    or attempt == CONTROL_PLANE_AUTH_ATTEMPTS
                ):
                    raise
                delay = min(2 ** (attempt - 1), 5)
                self.logger.warning(
                    "YiCloud control-plane authentication failed "
                    "transiently during %s; retrying in %ss "
                    "(attempt %s/%s): %s",
                    operation,
                    delay,
                    attempt,
                    CONTROL_PLANE_AUTH_ATTEMPTS,
                    error,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _wait_until_running(self, created: Any) -> Any:
        sandbox = self._sandbox_service
        assert sandbox is not None
        started_at = time.monotonic()
        deadline = started_at + self._ready_timeout_sec
        next_progress_log = started_at
        last_logged_state = ""
        current = created
        last_state = _state_of(current) or "unknown"
        last_reason = _status_reason_of(current)
        consecutive_status_errors = 0

        while time.monotonic() < deadline:
            try:
                current = await asyncio.to_thread(
                    sandbox.get_sandbox,
                    None,
                    sandbox.models.GetSandboxReq(
                        ProjectName=self._project_name,
                        SandboxId=self._sandbox_id,
                    ),
                )
            except Exception as error:
                consecutive_status_errors += 1
                if (
                    consecutive_status_errors
                    >= SANDBOX_STATUS_REQUEST_ATTEMPTS
                ):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(
                    2 ** (consecutive_status_errors - 1),
                    5,
                    remaining,
                )
                self.logger.warning(
                    "YiCloud Sandbox status request failed transiently; "
                    "retrying in %ss (attempt %s/%s) sandbox_id=%s: %s",
                    delay,
                    consecutive_status_errors,
                    SANDBOX_STATUS_REQUEST_ATTEMPTS,
                    self._sandbox_id,
                    error,
                )
                await asyncio.sleep(delay)
                continue
            consecutive_status_errors = 0
            now = time.monotonic()
            state = _state_of(current) or "unknown"
            reason = _status_reason_of(current)
            elapsed = max(0, int(now - started_at))
            if state != last_logged_state or now >= next_progress_log:
                self.logger.info(
                    "YiCloud Sandbox waiting sandbox_id=%s state=%s "
                    "elapsed_sec=%s ready_timeout_sec=%s reason=%s",
                    self._sandbox_id,
                    state,
                    elapsed,
                    self._ready_timeout_sec,
                    reason or "<none>",
                )
                last_logged_state = state
                next_progress_log = now + self._status_log_interval_sec
            last_state = state
            last_reason = reason
            if state == "running":
                return current
            if state in TERMINAL_FAILURE_STATES:
                raise RuntimeError(
                    f"YiCloud Sandbox {self._sandbox_id} entered terminal "
                    f"state={state}; reason={reason or '<none>'}"
                )
            await asyncio.sleep(3)

        elapsed = max(0, int(time.monotonic() - started_at))
        raise YiCloudSandboxReadyTimeoutError(
            f"YiCloud Sandbox {self._sandbox_id} scheduling timed out after "
            f"{elapsed}s (configured ready_timeout_sec="
            f"{self._ready_timeout_sec}); last_state={last_state}; "
            f"reason={last_reason or '<none>'}; environment_id="
            f"{self._environment_id}; image_ref={self._image_ref}"
        )

    def _ping_execd_sync(
        self,
        command_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        url = self._execd_url("/ping", command_url=command_url)
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            url,
            headers=self._signed_headers(
                "",
                url=url,
                access_token=access_token,
                accept="application/json",
            ),
            timeout=(10, 20),
        )
        response.raise_for_status()

    async def _wait_until_execd_ready(
        self, runtime: ServiceRuntime | None = None
    ) -> None:
        ready_timeout_sec = self._ready_timeout_sec
        deadline = time.monotonic() + ready_timeout_sec
        last_error: requests.RequestException | None = None
        sandbox_id = (
            runtime.sandbox_id
            if runtime is not None
            else getattr(self, "_sandbox_id", "")
        )
        command_url = (
            runtime.command_url
            if runtime is not None
            else getattr(self, "_command_url", "")
        )
        access_token = (
            runtime.access_token
            if runtime is not None
            else getattr(self, "_access_token", "")
        )
        if runtime is not None and not command_url:
            raise RuntimeError(
                f"OpenSandbox service {runtime.name!r} returned no command URL"
            )
        while time.monotonic() < deadline:
            try:
                await asyncio.to_thread(
                    self._ping_execd_sync,
                    command_url,
                    access_token,
                )
                return
            except requests.RequestException as error:
                last_error = error
                self.logger.warning(
                    "YiCloud Sandbox is running but execd is not ready; "
                    "retrying sandbox_id=%s: %s",
                    sandbox_id,
                    error,
                )
                await asyncio.sleep(3)
        raise RuntimeError(
            f"YiCloud Sandbox {sandbox_id} execd did not become ready "
            f"within {ready_timeout_sec}s: {last_error}"
        )

    def _detach_sandbox(self) -> None:
        self._sandbox_id = ""
        self._command_url = ""
        self._access_token = ""
        self._s3_downloader_ready = False

    async def _handle_start_failure(self) -> None:
        sandbox_id = self._sandbox_id
        if not sandbox_id:
            return
        if self._retain_on_start_failure:
            self.logger.warning(
                "YiCloud Sandbox retained after start failure for debugging: "
                "sandbox_id=%s environment_id=%s; automatic cleanup skipped",
                sandbox_id,
                self._environment_id,
            )
            self._detach_sandbox()
            return
        try:
            await self._delete_sandbox()
        except Exception as cleanup_error:  # noqa: BLE001
            # Preserve the original scheduling/start exception. Cleanup
            # can fail through any SDK/transport exception; keep it visible
            # without replacing the original start failure.
            self.logger.warning(
                "YiCloud Sandbox cleanup after start failure failed: "
                "sandbox_id=%s error=%s",
                sandbox_id,
                cleanup_error,
            )
            self._detach_sandbox()

    @staticmethod
    def _service_ports(spec: dict[str, Any]) -> list[int]:
        runtime = spec.get("runtime")
        if isinstance(runtime, dict) and "internal_ports" in runtime:
            ports: list[int] = []
            for item in runtime.get("internal_ports") or []:
                if not isinstance(item, dict):
                    raise TypeError("Bundle runtime.internal_ports entries must be objects")
                value = item.get("port")
                protocol = str(item.get("protocol", "tcp")).lower()
                if isinstance(value, int) and protocol == "tcp" and 1 <= value <= 65535:
                    ports.append(value)
                else:
                    raise RuntimeError("Bundle runtime.internal_ports has an invalid TCP port")
            return list(dict.fromkeys(ports))

        # Schema v1 compatibility only. Newly materialized schema v2 Bundles
        # must carry runtime.internal_ports from Compose expose/ports or OCI
        # image config and never rediscover them from Dockerfile text here.
        ports: list[int] = []
        for item in spec.get("ports") or []:
            value: Any = item.get("target") if isinstance(item, dict) else item
            if isinstance(value, int):
                ports.append(value)
                continue
            if isinstance(value, str):
                # Compose short syntax: host:container/proto and container/proto.
                candidate = value.split("/", 1)[0].rsplit(":", 1)[-1]
                if candidate.isdigit():
                    ports.append(int(candidate))
        return list(dict.fromkeys(port for port in ports if 1 <= port <= 65535))

    async def _wait_service_running(self, runtime: ServiceRuntime, created: Any) -> Any:
        sandbox = self._sandbox_service
        assert sandbox is not None
        deadline = time.monotonic() + self._ready_timeout_sec
        current = created
        consecutive_status_errors = 0
        while time.monotonic() < deadline:
            try:
                current = await asyncio.to_thread(
                    sandbox.get_sandbox,
                    None,
                    sandbox.models.GetSandboxReq(ProjectName=self._project_name, SandboxId=runtime.sandbox_id),
                )
            except Exception as error:
                consecutive_status_errors += 1
                if consecutive_status_errors >= SANDBOX_STATUS_REQUEST_ATTEMPTS:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(2 ** (consecutive_status_errors - 1), 5, remaining)
                self.logger.warning(
                    "YiCloud Sandbox status request failed transiently; "
                    "retrying in %ss (attempt %s/%s) sandbox_id=%s: %s",
                    delay,
                    consecutive_status_errors,
                    SANDBOX_STATUS_REQUEST_ATTEMPTS,
                    runtime.sandbox_id,
                    error,
                )
                await asyncio.sleep(delay)
                continue
            consecutive_status_errors = 0
            state = _state_of(current) or "unknown"
            if state == "running":
                return current
            if state in TERMINAL_FAILURE_STATES:
                raise RuntimeError(
                    f"OpenSandbox service {runtime.name!r} entered terminal state={state}; "
                    f"reason={_status_reason_of(current) or '<none>'}"
                )
            await asyncio.sleep(3)
        raise YiCloudSandboxReadyTimeoutError(
            f"OpenSandbox service {runtime.name!r} scheduling timed out after "
            f"{self._ready_timeout_sec}s"
        )

    def _service_image_source_ref(self, runtime: ServiceRuntime) -> str:
        image = runtime.spec.get("image") or {}
        return str(image.get("digest_ref") or image.get("sandbox_ref") or "").strip()

    def _service_image_ref(self, runtime: ServiceRuntime) -> str:
        return _yicloud_image_ref(self._service_image_source_ref(runtime))

    @staticmethod
    def _create_image_input(sandbox: Any, source_ref: str) -> Any:
        """Use URI for a fully-qualified OCI reference to pin its registry."""
        normalized = _yicloud_image_ref(source_ref)
        if normalized != source_ref:
            return sandbox.models.CreateSandboxReqImageInput(Uri=source_ref)
        return sandbox.models.CreateSandboxReqImageInput(Ref=source_ref)

    @staticmethod
    def _service_start_command(spec: dict[str, Any]) -> list[str] | None:
        """Map Compose entrypoint/command directly to Create.Entrypoint.

        This is not a hold/bootstrap wrapper or a process supervisor: when
        Compose supplied a start command, the platform starts that exact
        command. The common list forms preserve their argv boundaries.
        """
        runtime = spec.get("runtime")
        if isinstance(runtime, dict):
            argv = runtime.get("start_argv")
            if not isinstance(argv, list) or not argv or not all(
                isinstance(item, str) for item in argv
            ):
                raise RuntimeError(
                    "Bundle runtime.start_argv must be a non-empty string list"
                )
            return list(argv)

        entrypoint = spec.get("entrypoint")
        command = spec.get("command")
        if entrypoint is None and command is None:
            return None
        if entrypoint is None:
            if isinstance(command, list) and all(isinstance(item, str) for item in command):
                return list(command)
            if isinstance(command, str):
                return ["sh", "-c", command]
            raise RuntimeError("Compose command must be a string or string list")
        if isinstance(entrypoint, str):
            base = ["sh", "-c", entrypoint]
            if command is not None:
                raise RuntimeError(
                    "string Compose entrypoint with command has no lossless "
                    "OpenSandbox argv mapping"
                )
            return base
        if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
            raise RuntimeError("Compose entrypoint must be a string or string list")
        if command is None:
            return list(entrypoint)
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            return [*entrypoint, *command]
        if isinstance(command, str):
            return [*entrypoint, command]
        raise RuntimeError("Compose command must be a string or string list")

    @staticmethod
    def _hosts_update_command(entries: list[str]) -> str:
        block = "\n".join(entries)
        encoded = base64.b64encode(block.encode("utf-8")).decode("ascii")
        return (
            "tmp=$(mktemp); "
            f"printf %s {shlex.quote(encoded)} | base64 -d >$tmp; "
            f"sed '/^{HOSTS_BLOCK_BEGIN}$/,/^{HOSTS_BLOCK_END}$/d' /etc/hosts >$tmp.hosts; "
            f"{{ cat $tmp.hosts; printf '{HOSTS_BLOCK_BEGIN}\\n'; cat $tmp; printf '\\n{HOSTS_BLOCK_END}\\n'; }} >/etc/hosts; "
            "rm -f $tmp $tmp.hosts"
        )

    def _service_entrypoint(self, runtime: ServiceRuntime) -> list[str] | None:
        start_command = self._service_start_command(runtime.spec)
        if len(self._services) <= 1:
            return start_command
        if start_command is None:
            raise RuntimeError(
                "OpenSandbox multi-service startup requires an explicit start command"
            )
        # Compose service discovery is independent of depends_on. Hold every
        # process until the controller has addresses for all peer aliases.
        marker = shlex.quote(
            f"{COMPOSE_START_MARKER_PREFIX}{runtime.sandbox_name}"
        )
        return [
            "sh",
            "-c",
            f'set -e\nwhile [ ! -e {marker} ]; do sleep 1; done\nexec "$@"',
            "harbor-compose-entrypoint",
            *start_command,
        ]

    def _make_service_sandbox_name(self, service: str) -> str:
        base = f"{self._make_sandbox_name()}-{service}".lower()
        return re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:63].rstrip("-")

    async def _wait_for_sandbox_ids_absent(self, sandbox_ids: set[str]) -> bool:
        """Return only after the control-plane list no longer contains IDs."""
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_ids:
            return True
        deadline = time.monotonic() + self._cleanup_wait_sec
        while time.monotonic() < deadline:
            remaining = await self._listed_sandbox_ids(sandbox_ids)
            if not remaining:
                return True
            await asyncio.sleep(3)
        return False

    async def _listed_sandbox_ids(self, sandbox_ids: set[str]) -> set[str]:
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_ids:
            return set()
        remaining: set[str] = set()
        offset = 0
        while True:
            listed = await asyncio.to_thread(
                sandbox.list_sandboxes,
                None,
                sandbox.models.ListSandboxesReq(
                    ProjectName=self._project_name,
                    EnvironmentId=self._environment_id,
                    Limit=100,
                    Offset=offset,
                ),
            )
            items = list(getattr(listed, "Items", None) or [])
            remaining.update(
                str(getattr(item, "Id", "") or "")
                for item in items
                if str(getattr(item, "Id", "") or "") in sandbox_ids
            )
            if len(items) < 100:
                return remaining
            offset += len(items)

    async def _request_sandbox_deletion(
        self,
        operation: str,
        sandbox_ids: set[str],
        function: Any,
        request: Any,
    ) -> tuple[bool, Any]:
        """Retry an idempotent delete and confirm ambiguous responses by list."""
        for attempt in range(1, CONTROL_PLANE_AUTH_ATTEMPTS + 1):
            try:
                response = await asyncio.to_thread(function, None, request)
                return True, response
            except Exception as error:
                try:
                    if not await self._listed_sandbox_ids(sandbox_ids):
                        self.logger.warning(
                            "YiCloud Sandbox %s response failed after the "
                            "Sandbox disappeared; treating cleanup as complete: "
                            "sandbox_ids=%s error=%s",
                            operation,
                            sorted(sandbox_ids),
                            error,
                        )
                        return False, None
                except Exception:  # noqa: BLE001, S110 - retry idempotent delete
                    pass
                if attempt == CONTROL_PLANE_AUTH_ATTEMPTS:
                    raise
                delay = min(2 ** (attempt - 1), 5)
                self.logger.warning(
                    "YiCloud Sandbox %s failed transiently; retrying in %ss "
                    "(attempt %s/%s) sandbox_ids=%s: %s",
                    operation,
                    delay,
                    attempt,
                    CONTROL_PLANE_AUTH_ATTEMPTS,
                    sorted(sandbox_ids),
                    error,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _batch_delete_sandboxes(self, sandbox_ids: set[str]) -> bool:
        """Request physical removal, then require list invisibility as success."""
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_ids:
            return True
        requested, response = await self._request_sandbox_deletion(
            "batch deletion",
            sandbox_ids,
            sandbox.batch_delete_sandboxes,
            sandbox.models.BatchDeleteSandboxesReq(
                ProjectName=self._project_name,
                Ids=sorted(sandbox_ids),
            ),
        )
        if not requested:
            return True
        failed = {
            str(getattr(item, "Id", "") or "")
            for item in (getattr(response, "Failed", None) or [])
        }
        if failed:
            self.logger.warning(
                "YiCloud Sandbox batch deletion rejected IDs: %s", sorted(failed)
            )
            return False
        return await self._wait_for_sandbox_ids_absent(sandbox_ids)

    async def _delete_single_sandbox(self, sandbox_id: str) -> bool:
        """Delete one Sandbox through the provider's dedicated single-ID API."""
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_id:
            return True
        requested, _response = await self._request_sandbox_deletion(
            "deletion",
            {sandbox_id},
            sandbox.delete_sandbox,
            sandbox.models.DeleteSandboxReq(
                ProjectName=self._project_name,
                SandboxId=sandbox_id,
            ),
        )
        if not requested:
            return True
        return await self._wait_for_sandbox_ids_absent({sandbox_id})

    async def _delete_service(self, runtime: ServiceRuntime) -> None:
        """Delete one composite service and invalidate its runtime connection."""
        sandbox_id = runtime.sandbox_id
        try:
            if not sandbox_id:
                runtime.state = "DELETED"
                return
            self.logger.info(
                "YiCloud composite service cleanup requested service=%s sandbox_id=%s",
                runtime.name,
                sandbox_id,
            )
            deleted = await self._delete_single_sandbox(sandbox_id)
            runtime.state = "DELETED" if deleted else "DELETE_UNCONFIRMED"
            if deleted:
                runtime.sandbox_id = ""
                self.logger.info(
                    "YiCloud composite service cleanup confirmed service=%s sandbox_id=%s",
                    runtime.name,
                    sandbox_id,
                )
            else:
                self.logger.warning(
                    "YiCloud composite service deletion was not confirmed within %ss: "
                    "service=%s sandbox_id=%s",
                    self._cleanup_wait_sec,
                    runtime.name,
                    sandbox_id,
                )
        finally:
            runtime.command_url = ""
            runtime.access_token = ""
            runtime.internal_address = ""
            if runtime.name == self._main_service:
                self._detach_sandbox()

    async def _delete_service_group(self) -> None:
        runtimes = [runtime for runtime in self._services.values() if runtime.sandbox_id]
        try:
            sandbox_ids = {runtime.sandbox_id for runtime in runtimes}
            if len(sandbox_ids) == 1:
                deleted = await self._delete_single_sandbox(next(iter(sandbox_ids)))
            else:
                deleted = await self._batch_delete_sandboxes(sandbox_ids)
            for runtime in runtimes:
                runtime.state = "DELETED" if deleted else "DELETE_UNCONFIRMED"
            if not deleted:
                self.logger.warning(
                    "YiCloud composite Sandbox deletion was not confirmed within %ss: %s",
                    self._cleanup_wait_sec,
                    [runtime.sandbox_id for runtime in runtimes],
                )
        finally:
            self._detach_sandbox()

    async def _run_service_command(
        self, runtime: ServiceRuntime, command: str, *, cwd: str = "/", timeout_sec: int = 60
    ) -> ExecResult:
        if not runtime.command_url or not runtime.access_token:
            raise RuntimeError(f"OpenSandbox service {runtime.name!r} is not running")
        wrapped = (
            "set +e\n(\n" + command + "\n)\nharbor_rc=$?\n"
            f"printf '\\n{EXIT_MARKER}%s\\n' \"$harbor_rc\"\nexit \"$harbor_rc\"\n"
        )
        body = json.dumps({"command": wrapped, "background": False, "timeout": timeout_sec * 1000, "cwd": cwd}, separators=(",", ":"))
        now = time.localtime()
        headers = {
            "X-OGW-PUBLIC-KEY": self._base_client.crede.public_key,
            "X-OGW-TICK": str(int(time.mktime(now) * 1000)),
            "X-OGW-SIGN": self._base_client.crede.sign(now, urlsplit(runtime.command_url).query, body),
            "X-Sandbox-Access-Token": runtime.access_token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        def run() -> ExecResult:
            session = requests.Session()
            session.trust_env = False
            response = session.post(runtime.command_url, headers=headers, data=body, timeout=timeout_sec + 60)
            response.raise_for_status()
            events = _parse_sse(response.text)
            stdout = "".join(str(event.get("text", "")) for event in events if event.get("type") == "stdout")
            stderr = "".join(str(event.get("text", "")) for event in events if event.get("type") == "stderr")
            matches = re.findall(rf"{re.escape(EXIT_MARKER)}(\d+)", stdout)
            return ExecResult(
                stdout=re.sub(rf"\n?{re.escape(EXIT_MARKER)}\d+\n?", "", stdout),
                stderr=stderr,
                return_code=int(matches[-1]) if matches else 1,
            )
        return await asyncio.to_thread(run)

    async def _wire_service_aliases(
        self, runtimes: list[ServiceRuntime] | None = None
    ) -> None:
        entries: list[str] = []
        for target in self._services.values():
            aliases = [target.name, *(target.spec.get("aliases") or [])]
            aliases = list(dict.fromkeys(str(item) for item in aliases if str(item)))
            if not target.internal_address:
                continue
            entries.append(f"{target.internal_address} {' '.join(aliases)}")
        command = self._hosts_update_command(entries)
        targets = runtimes if runtimes is not None else list(self._services.values())
        for runtime in targets:
            result = await self._run_service_command(runtime, command, timeout_sec=60)
            if result.return_code != 0:
                raise RuntimeError(f"failed to inject service aliases into {runtime.name!r}: {result.stderr or result.stdout}")

    async def _release_service_entrypoint(self, runtime: ServiceRuntime) -> None:
        marker = shlex.quote(
            f"{COMPOSE_START_MARKER_PREFIX}{runtime.sandbox_name}"
        )
        result = await self._run_service_command(
            runtime, f"touch {marker}", timeout_sec=60
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to start OpenSandbox service {runtime.name!r}: "
                f"{result.stderr or result.stdout}"
            )

    @staticmethod
    def _service_healthcheck_command(runtime: ServiceRuntime) -> str | None:
        contract = runtime.spec.get("runtime") or {}
        readiness = contract.get("readiness") if isinstance(contract, dict) else None
        if not isinstance(readiness, dict) or readiness.get("type") != "healthcheck":
            return None
        healthcheck = readiness.get("healthcheck")
        if not isinstance(healthcheck, dict):
            raise TypeError(f"OpenSandbox healthcheck readiness is invalid for {runtime.name!r}")
        test = healthcheck.get("test")
        if isinstance(test, list):
            if test and test[0] == "CMD":
                return " ".join(shlex.quote(str(item)) for item in test[1:])
            return " ".join(str(item) for item in test[1:])
        if isinstance(test, str):
            return test
        return None

    def _service_start_order(self) -> list[ServiceRuntime]:
        ordered: list[ServiceRuntime] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise RuntimeError(
                    f"OpenSandbox service dependencies contain a cycle at {name!r}"
                )
            runtime = self._services.get(name)
            if runtime is None:
                raise RuntimeError(f"OpenSandbox service dependency is missing: {name!r}")
            dependencies = runtime.spec.get("depends_on") or {}
            if not isinstance(dependencies, dict):
                raise TypeError(f"OpenSandbox depends_on is invalid for {name!r}")
            visiting.add(name)
            for dependency_name, config in dependencies.items():
                if not isinstance(config, dict):
                    raise TypeError(
                        "OpenSandbox dependency config is invalid: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                condition = str(config.get("condition") or "service_started")
                if condition not in {"service_started", "service_healthy"}:
                    raise RuntimeError(
                        f"OpenSandbox dependency condition is unsupported: "
                        f"{name!r} -> {dependency_name!r} uses {condition!r}"
                    )
                if not config.get("required", True):
                    raise RuntimeError(
                        f"OpenSandbox optional service dependency is unsupported: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                dependency = self._services.get(str(dependency_name))
                if dependency is None:
                    raise RuntimeError(
                        f"OpenSandbox service dependency is missing: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                if (
                    condition == "service_healthy"
                    and self._service_healthcheck_command(dependency) is None
                ):
                    raise RuntimeError(
                        f"OpenSandbox service_healthy dependency has no healthcheck: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                visit(str(dependency_name))
            visiting.remove(name)
            visited.add(name)
            ordered.append(runtime)

        for service_name in self._services:
            visit(service_name)
        return ordered

    async def _wait_service_healthcheck(
        self, runtime: ServiceRuntime, deadline: float
    ) -> None:
        command = self._service_healthcheck_command(runtime)
        if command is None:
            return
        last_error = ""
        while time.monotonic() < deadline:
            result = await self._run_service_command(
                runtime,
                command,
                timeout_sec=60,
            )
            if result.return_code == 0:
                return
            last_error = result.stderr or result.stdout
            now = time.monotonic()
            if now < deadline:
                await asyncio.sleep(min(2, deadline - now))
        raise RuntimeError(
            f"OpenSandbox healthcheck failed for {runtime.name!r} "
            f"within {self._ready_timeout_sec}s: {last_error}"
        )

    async def _wait_bundle_ready(
        self, already_healthy: set[str] | None = None
    ) -> None:
        healthy = already_healthy or set()
        healthcheck_deadline = time.monotonic() + self._ready_timeout_sec
        for runtime in self._services.values():
            if runtime.name in healthy:
                continue
            await self._wait_service_healthcheck(runtime, healthcheck_deadline)
        # The runtime contract distinguishes address-discovery ports from
        # readiness.  Only an explicit TCP readiness declaration is probed;
        # merely exposing a port does not promise that the service is ready.
        main = self._services[self._main_service]
        deadline = time.monotonic() + self._ready_timeout_sec
        for runtime in self._services.values():
            contract = runtime.spec.get("runtime") or {}
            readiness = contract.get("readiness") if isinstance(contract, dict) else None
            if not isinstance(readiness, dict) or readiness.get("type") != "tcp":
                continue
            port = readiness.get("port")
            if not isinstance(port, int) or port not in self._service_ports(runtime.spec):
                raise RuntimeError(f"OpenSandbox TCP readiness is invalid for {runtime.name!r}")
            probe = (
                "if command -v nc >/dev/null 2>&1; then nc -z -w 2 \"$@\"; "
                "elif command -v bash >/dev/null 2>&1; then timeout 3 bash -c "
                "'cat < /dev/null > /dev/tcp/$1/$2' _ \"$@\"; "
                "elif command -v python3 >/dev/null 2>&1; then python3 -c "
                "'import socket,sys;s=socket.create_connection((sys.argv[1],int(sys.argv[2])),2);s.close()' \"$@\"; "
                "else exit 69; fi"
            )
            while True:
                result = await self._run_service_command(
                    main,
                    f"sh -c {shlex.quote(probe)} _ {shlex.quote(runtime.name)} {port}",
                    timeout_sec=15,
                )
                if result.return_code == 0:
                    break
                if result.return_code == 69:
                    raise RuntimeError(
                        "OpenSandbox cannot probe required service port: "
                        "main has none of nc, bash, or python3"
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"OpenSandbox required port was not ready: "
                        f"service={runtime.name} port={port}; "
                        f"stderr={result.stderr or result.stdout}"
                    )
                await asyncio.sleep(2)

    async def _start_composite(self) -> None:
        sandbox = self._sandbox_service
        assert sandbox is not None
        start_order = self._service_start_order()
        healthy: set[str] = set()
        for runtime in start_order:
            runtime.sandbox_name = self._make_service_sandbox_name(runtime.name)
            image_ref = self._service_image_source_ref(runtime)
            request: dict[str, Any] = {
                "ProjectName": self._project_name,
                "EnvironmentId": self._environment_id,
                "Name": runtime.sandbox_name,
                "Image": self._create_image_input(sandbox, image_ref),
                "Env": {
                    **self._persistent_env,
                    **{
                        str(key): str(value)
                        for key, value in (runtime.spec.get("environment") or {}).items()
                        if value is not None
                    },
                },
                "Resources": sandbox.models.CreateSandboxReqResources(Cpu=self._request_cpu, Memory=self._request_memory),
                "LifecycleMinutes": self._lifecycle_minutes,
                "RequestTimeoutSeconds": self._request_timeout_sec,
            }
            start_command = self._service_entrypoint(runtime)
            if start_command is not None:
                request["Entrypoint"] = start_command
            ports = self._service_ports(runtime.spec)
            if ports:
                request["Ports"] = [sandbox.models.CreateSandboxReqPort(ContainerPort=port, Name=f"svc-{port}", Purpose=f"Harbor {runtime.name}") for port in ports]
            created = await self._retry_control_plane_auth(
                f"create composite service {runtime.name}",
                sandbox.create_sandbox,
                None,
                sandbox.models.CreateSandboxReq(**request),
            )
            runtime.sandbox_id = str(getattr(created, "Id", "") or "")
            if not runtime.sandbox_id:
                raise RuntimeError(f"OpenSandbox create returned no sandbox ID for service {runtime.name!r}")
            current = await self._wait_service_running(runtime, created)
            _validate_sandbox_binding(current, self._environment_id, self._service_image_ref(runtime))
            runtime.access_token = _access_token_of(created) or _access_token_of(current)
            runtime.command_url = _command_url_of(current)
            runtime.internal_address = _internal_address_of(current)
            runtime.state = "WIRING"
            if not runtime.access_token:
                raise RuntimeError(f"OpenSandbox service {runtime.name!r} returned no access token")
            await self._wait_until_execd_ready(runtime)
        await self._wire_service_aliases()
        for runtime in start_order:
            dependencies = runtime.spec.get("depends_on") or {}
            for dependency_name, config in dependencies.items():
                if (
                    config.get("condition") == "service_healthy"
                    and dependency_name not in healthy
                ):
                    await self._wait_service_healthcheck(
                        self._services[dependency_name],
                        time.monotonic() + self._ready_timeout_sec,
                    )
                    healthy.add(dependency_name)
            await self._release_service_entrypoint(runtime)
            runtime.state = "STARTING"
        await self._wait_bundle_ready(healthy)
        main = self._services[self._main_service]
        self._sandbox_id, self._sandbox_name = main.sandbox_id, main.sandbox_name
        self._access_token, self._command_url = main.access_token, main.command_url
        for runtime in self._services.values():
            runtime.state = "READY"

    async def start(self, force_build: bool) -> None:
        if force_build:
            self.logger.warning(
                "YiCloud OpenSandbox ignores force_build and uses prebuilt images"
            )
        self._validate_definition()
        self._initialize_service()
        sandbox = self._sandbox_service
        assert sandbox is not None
        await self._retry_control_plane_auth(
            "resolve environment",
            self._resolve_environment_id,
        )
        if self._bundle is not None:
            try:
                await self._start_composite()
                setup = await self.exec(
                    "mkdir -p /logs/agent /logs/verifier /logs/artifacts /artifacts && "
                    "chmod 777 /logs/agent /logs/verifier /logs/artifacts /artifacts",
                    cwd="/",
                    timeout_sec=60,
                    user="root",
                )
                if setup.return_code != 0:
                    raise RuntimeError(
                        "failed to initialize Harbor log directories: "
                        f"{setup.stderr or setup.stdout}"
                    )
                await self._upload_environment_dir_after_start()
                await self._materialize_read_only_mounts()
            except BaseException:
                await self._delete_service_group()
                raise
            return
        self._sandbox_name = self._make_sandbox_name()

        created = await self._retry_control_plane_auth(
            "create sandbox",
            sandbox.create_sandbox,
            None,
            sandbox.models.CreateSandboxReq(
                ProjectName=self._project_name,
                EnvironmentId=self._environment_id,
                Name=self._sandbox_name,
                Image=self._create_image_input(sandbox, self._image_source_ref),
                Entrypoint=["sh", "-c", "while :; do sleep 60; done"],
                Env=dict(self._persistent_env),
                Resources=sandbox.models.CreateSandboxReqResources(
                    Cpu=self._request_cpu,
                    Memory=self._request_memory,
                ),
                LifecycleMinutes=self._lifecycle_minutes,
                RequestTimeoutSeconds=self._request_timeout_sec,
            ),
        )
        self._sandbox_id = str(getattr(created, "Id", "") or "")
        if not self._sandbox_id:
            raise RuntimeError("YiCloud CreateSandbox returned no sandbox ID")
        created_token = _access_token_of(created)
        self.logger.info(
            "YiCloud Sandbox create accepted sandbox_id=%s "
            "environment_id=%s environment_name=%s image_ref=%s "
            "cpu=%s memory=%s ready_timeout_sec=%s "
            "lifecycle_minutes=%s retain_on_start_failure=%s",
            self._sandbox_id,
            self._environment_id,
            self._environment_name,
            self._image_ref,
            self._request_cpu,
            self._request_memory,
            self._ready_timeout_sec,
            self._lifecycle_minutes,
            self._retain_on_start_failure,
        )

        try:
            current = await self._wait_until_running(created)
            _validate_sandbox_binding(
                current, self._environment_id, self._image_ref
            )
            self.logger.info(
                "YiCloud Sandbox binding confirmed environment_id=%s "
                "environment_name=%s image_ref=%s",
                self._environment_id,
                self._environment_name,
                self._image_ref,
            )
            self._access_token = created_token or _access_token_of(current)
            if not self._access_token:
                raise RuntimeError("YiCloud Sandbox returned no access token")
            self._command_url = _command_url_of(current)
            await self._wait_until_execd_ready()
            setup = await self.exec(
                "mkdir -p /logs/agent /logs/verifier /logs/artifacts /artifacts && "
                "chmod 777 /logs/agent /logs/verifier /logs/artifacts /artifacts",
                cwd="/",
                timeout_sec=60,
                user="root",
            )
            if setup.return_code != 0:
                raise RuntimeError(
                    "failed to initialize Harbor log directories: "
                    f"{setup.stderr or setup.stdout}"
                )
            await self._upload_environment_dir_after_start()
            await self._materialize_read_only_mounts()
        except BaseException:
            await self._handle_start_failure()
            raise

    async def _delete_sandbox(self) -> None:
        sandbox = self._sandbox_service
        sandbox_id = self._sandbox_id
        if sandbox is None or not sandbox_id:
            return
        try:
            self.logger.info(
                "YiCloud Sandbox cleanup requested sandbox_id=%s", sandbox_id
            )
            deleted = await self._delete_single_sandbox(sandbox_id)
            if deleted:
                self.logger.info(
                    "YiCloud Sandbox cleanup confirmed sandbox_id=%s", sandbox_id
                )
            else:
                self.logger.warning(
                    "YiCloud Sandbox deletion not confirmed within %ss: %s",
                    self._cleanup_wait_sec,
                    sandbox_id,
                )
        finally:
            self._detach_sandbox()

    async def stop(self, delete: bool) -> None:
        if self._bundle is not None:
            if delete:
                await self._delete_service_group()
            else:
                self._detach_sandbox()
            return
        if self._retain_after_trial and self._sandbox_id:
            self.logger.warning(
                "YiCloud Sandbox retained after trial sandbox_id=%s "
                "environment_id=%s; it remains subject to platform lifecycle expiry",
                self._sandbox_id,
                self._environment_id,
            )
            self._detach_sandbox()
            return
        if delete:
            await self._delete_sandbox()
        else:
            self._detach_sandbox()

    def _signed_headers(
        self,
        body: str | bytes,
        *,
        url: str | None = None,
        access_token: str | None = None,
        content_type: str = "application/json",
        accept: str = "text/event-stream",
    ) -> dict[str, str]:
        if self._base_client is None:
            raise RuntimeError("YiCloud client is not initialized")
        now = time.localtime()
        timestamp = str(int(time.mktime(now) * 1000))
        query = urlsplit(url or self._command_url).query
        signature = self._base_client.crede.sign(now, query, body)
        return {
            "X-OGW-PUBLIC-KEY": self._base_client.crede.public_key,
            "X-OGW-TICK": timestamp,
            "X-OGW-SIGN": signature,
            "X-Sandbox-Access-Token": (
                self._access_token if access_token is None else access_token
            ),
            "Content-Type": content_type,
            "Accept": accept,
        }

    def _execd_url(
        self, suffix: str, *, command_url: str | None = None
    ) -> str:
        source_url = self._command_url if command_url is None else command_url
        if not source_url:
            raise RuntimeError("YiCloud Sandbox is not running")
        parsed = urlsplit(source_url)
        base_path = parsed.path.rsplit("/", 1)[0]
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{base_path}/{suffix.lstrip('/')}",
                parsed.query,
                "",
            )
        )

    def _upload_chunk_sync(
        self,
        content: bytes,
        target_path: str,
        filename: str,
    ) -> None:
        upload_url = self._execd_url("/files/upload")
        encoded_content = base64.b64encode(content)
        metadata = json.dumps(
            {
                "path": target_path,
                # Execd parses this value as octal text, so pass 600 rather
                # than Python's decimal representation of 0o600 (384).
                "mode": 600,
            },
            separators=(",", ":"),
        )
        attempts = _execd_request_attempts()
        for attempt in range(attempts):
            session = requests.Session()
            session.trust_env = False
            request = requests.Request(
                "POST",
                upload_url,
                files=[
                    (
                        "metadata",
                        (
                            "metadata.json",
                            io.BytesIO(metadata.encode()),
                            "application/json",
                        ),
                    ),
                    (
                        "file",
                        (
                            f"{filename}.b64",
                            io.BytesIO(encoded_content),
                            "text/plain",
                        ),
                    ),
                ],
            )
            prepared = session.prepare_request(request)
            if not isinstance(prepared.body, bytes):
                raise TypeError(
                    "execd multipart upload did not produce a binary body"
                )
            content_type = prepared.headers.get("Content-Type", "")
            prepared.headers.update(
                self._signed_headers(
                    prepared.body.decode("ascii"),
                    url=upload_url,
                    content_type=content_type,
                    accept="application/json",
                )
            )
            try:
                response = session.send(
                    prepared,
                    timeout=self._request_timeout_sec + 300,
                )
                if response.ok:
                    return
                raise requests.HTTPError(
                    f"YiCloud execd file upload returned {response.status_code}",
                    response=response,
                )
            except requests.RequestException as error:
                if (
                    not _retryable_execd_error(error)
                    or attempt + 1 == attempts
                ):
                    response = getattr(error, "response", None)
                    if response is not None:
                        raise RuntimeError(
                            "YiCloud execd file upload failed "
                            f"status={response.status_code} "
                            f"body={response.text[:1000]!r}"
                        ) from error
                    raise
                delay = min(2**attempt, 5)
                self.logger.warning(
                    "YiCloud execd file upload failed transiently; "
                    "retrying in %ss (attempt %s/%s): %s",
                    delay,
                    attempt + 1,
                    attempts,
                    error,
                )
                time.sleep(delay)

    def _fast_upload_url(self) -> str:
        if not self._sandbox_id:
            raise RuntimeError("YiCloud Sandbox is not running")
        origin = os.environ.get(
            "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", ""
        ).strip()
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN must be an HTTP(S) "
                    "origin"
                )
            return (
                f"{origin.rstrip('/')}/sandboxes/{self._sandbox_id}"
                "/proxy/44772/files/upload"
            )

        command_url = getattr(self, "_command_url", "")
        if not command_url:
            return ""
        parsed = urlsplit(command_url)
        command_suffix = "/command"
        if not parsed.path.endswith(command_suffix):
            raise RuntimeError(
                "YiCloud execd command endpoint has an unexpected path: "
                f"{parsed.path!r}"
            )
        path = parsed.path[: -len(command_suffix)] + "/files/upload"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _upload_file_fast_sync(
        self,
        source: Path,
        target_path: str,
        upload_url: str,
    ) -> None:
        timeout_sec = _positive_int(
            os.environ.get("YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC", "1800"),
            "YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC",
        )
        metadata_payload = {
            "path": target_path,
            "owner": "root",
            "group": "root",
            "mode": int(f"{stat.S_IMODE(source.stat().st_mode):o}"),
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="yicloud-fast-upload-") as tmp:
            tmp_dir = Path(tmp)
            metadata = tmp_dir / "metadata.json"
            headers = tmp_dir / "headers.txt"
            response_body = tmp_dir / "response.txt"
            metadata.write_text(
                json.dumps(metadata_payload, separators=(",", ":")),
                encoding="utf-8",
            )
            headers.write_text(
                f"X-Sandbox-Access-Token: {self._access_token}\n",
                encoding="utf-8",
            )
            headers.chmod(0o600)
            completed = subprocess.run(
                [
                    "curl",
                    "--noproxy",
                    "*",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    str(timeout_sec),
                    "--request",
                    "POST",
                    upload_url,
                    "--header",
                    f"@{headers}",
                    "--form",
                    (
                        f"metadata=@{metadata};type=application/json;"
                        "filename=metadata.json"
                    ),
                    "--form",
                    f"file=@{source};filename={source.name}",
                    "--output",
                    str(response_body),
                    "--write-out",
                    "%{http_code}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec + 30,
                check=False,
            )
            status_text = completed.stdout.strip()
            status = int(status_text) if status_text.isdigit() else 0
            if completed.returncode != 0 or not 200 <= status < 300:
                body = (
                    response_body.read_text(
                        encoding="utf-8", errors="replace"
                    )[:1000]
                    if response_body.exists()
                    else ""
                )
                raise RuntimeError(
                    "YiCloud fast file upload failed "
                    f"curl_rc={completed.returncode} status={status} "
                    f"stderr={completed.stderr.strip()[:1000]!r} body={body!r}"
                )
        elapsed = max(time.monotonic() - started, 0.001)
        size = source.stat().st_size
        self.logger.info(
            "YiCloud fast upload complete path=%s size_bytes=%s "
            "elapsed_seconds=%.3f speed_mib_per_second=%.2f",
            target_path,
            size,
            elapsed,
            size / elapsed / 1024 / 1024,
        )

    async def _upload_file_fast(
        self,
        source: Path,
        target_path: str,
        upload_url: str,
        mode: str,
    ) -> None:
        use_signed_execd = not os.environ.get(
            "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", ""
        ).strip()
        if (
            not use_signed_execd
            and source.stat().st_size <= FAST_UPLOAD_CHUNK_BYTES
        ):
            await asyncio.to_thread(
                self._upload_file_fast_sync,
                source,
                target_path,
                upload_url,
            )
            return

        remote_prefix = f"/tmp/harbor-fast-upload-{time.time_ns()}"
        remote_parts: list[str] = []
        handle = None
        try:
            handle = await asyncio.to_thread(source.open, "rb")
            with tempfile.TemporaryDirectory(
                prefix="yicloud-fast-upload-parts-"
            ) as tmp:
                tmp_dir = Path(tmp)
                index = 0
                while chunk := await asyncio.to_thread(
                    handle.read, FAST_UPLOAD_CHUNK_BYTES
                ):
                    remote_part = f"{remote_prefix}-{index:04d}"
                    if use_signed_execd:
                        await asyncio.to_thread(
                            self._upload_chunk_sync,
                            chunk,
                            remote_part,
                            source.name,
                        )
                    else:
                        local_part = tmp_dir / f"part-{index:04d}"
                        await asyncio.to_thread(local_part.write_bytes, chunk)
                        await asyncio.to_thread(
                            self._upload_file_fast_sync,
                            local_part,
                            remote_part,
                            upload_url,
                        )
                    remote_parts.append(remote_part)
                    index += 1

            quoted_target = shlex.quote(target_path)
            if use_signed_execd:
                assemble = f": > {quoted_target}"
                for part in remote_parts:
                    assemble += (
                        f" && base64 -d {shlex.quote(part)} >> {quoted_target}"
                    )
                assemble += f" && chmod {mode} {quoted_target}"
            else:
                quoted_parts = " ".join(
                    shlex.quote(part) for part in remote_parts
                )
                assemble = (
                    f"cat {quoted_parts} > {quoted_target} && "
                    f"chmod {mode} {quoted_target}"
                )
            assembled = await self.exec(
                assemble,
                cwd="/",
                timeout_sec=300,
                user="root",
            )
            if assembled.return_code != 0:
                raise RuntimeError(
                    f"failed to assemble fast upload at {target_path!r}: "
                    f"{assembled.stderr or assembled.stdout}"
                )
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            if remote_parts:
                await self.exec(
                    "rm -f "
                    + " ".join(shlex.quote(part) for part in remote_parts),
                    cwd="/",
                    timeout_sec=60,
                    user="root",
                )

    def _cleanup_remote_exec_paths_sync(self, *paths: str) -> None:
        if not paths or not self._command_url or not self._access_token:
            return
        command = "rm -rf " + " ".join(shlex.quote(path) for path in paths)
        payload = {
            "command": command,
            "background": False,
            "timeout": 30_000,
            "uid": 0,
        }
        body = json.dumps(payload, separators=(",", ":"))
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.post(
                self._command_url,
                headers=self._signed_headers(body),
                data=body,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001 - cleanup is best effort
            self.logger.warning(
                "YiCloud failed to remove remote exec temporary files: %s",
                error,
            )

    def _run_command_direct_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        timeout_sec: int | None,
        uid: int | None = None,
    ) -> ExecResult:
        if not self._command_url or not self._access_token:
            raise RuntimeError("YiCloud Sandbox is not running")
        command_id = uuid.uuid4().hex
        prefix = f"/tmp/harbor-execd-{command_id}"
        stdout_path = f"{prefix}.stdout"
        stderr_path = f"{prefix}.stderr"
        status_path = f"{prefix}.status"
        lock_path = f"{prefix}.lock"
        lock_dir = f"{lock_path}.d"
        cleanup_paths = (
            lock_path,
            lock_dir,
            stdout_path,
            stderr_path,
            status_path,
            f"{status_path}.tmp",
        )
        wrapped = (
            "set +e\n"
            f"harbor_lock_dir={shlex.quote(lock_dir)}\n"
            "if command -v flock >/dev/null 2>&1; then\n"
            f"  exec 9>{shlex.quote(lock_path)}\n"
            "  flock -x 9\n"
            "else\n"
            "  while ! mkdir \"$harbor_lock_dir\" 2>/dev/null; do\n"
            f"    [ -f {shlex.quote(status_path)} ] && break\n"
            "    if [ -s \"$harbor_lock_dir/pid\" ]; then\n"
            "      harbor_lock_pid=$(cat \"$harbor_lock_dir/pid\")\n"
            "      if ! kill -0 \"$harbor_lock_pid\" 2>/dev/null; then\n"
            "        rm -rf \"$harbor_lock_dir\"\n"
            "        continue\n"
            "      fi\n"
            "    fi\n"
            "    sleep 1\n"
            "  done\n"
            f"  if [ ! -f {shlex.quote(status_path)} ] "
            "&& [ -d \"$harbor_lock_dir\" ]; then\n"
            "    printf '%s\\n' \"$$\" > \"$harbor_lock_dir/pid\"\n"
            "    trap 'rm -rf \"$harbor_lock_dir\"' EXIT\n"
            "    trap 'exit 129' HUP\n"
            "    trap 'exit 130' INT\n"
            "    trap 'exit 143' TERM\n"
            "  fi\n"
            "fi\n"
            f"if [ ! -f {shlex.quote(status_path)} ]; then\n"
            "  (\n"
            f"{command}\n"
            f"  ) > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}\n"
            "  harbor_rc=$?\n"
            f"  printf '%s\\n' \"$harbor_rc\" > {shlex.quote(status_path)}.tmp\n"
            f"  mv -f {shlex.quote(status_path)}.tmp {shlex.quote(status_path)}\n"
            "fi\n"
            f"cat {shlex.quote(stdout_path)} 2>/dev/null\n"
            f"cat {shlex.quote(stderr_path)} >&2 2>/dev/null\n"
            f"harbor_rc=$(cat {shlex.quote(status_path)})\n"
            f"printf '\\n{EXIT_MARKER}%s\\n' \"$harbor_rc\"\n"
            "exit \"$harbor_rc\"\n"
        )
        effective_timeout_sec = timeout_sec or 3600
        payload = {
            "command": wrapped,
            "background": False,
            "timeout": effective_timeout_sec * 1000,
            "envs": env,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        if uid is not None:
            payload["uid"] = uid
        body = json.dumps(payload, separators=(",", ":"))
        request_timeout_sec = effective_timeout_sec + 60
        attempts = _execd_request_attempts()
        for attempt in range(attempts):
            try:
                session = requests.Session()
                session.trust_env = False
                response = session.post(
                    self._command_url,
                    headers=self._signed_headers(body),
                    data=body,
                    timeout=request_timeout_sec,
                )
                response.raise_for_status()
                response_text = response.text
                break
            except requests.RequestException as error:
                if (
                    not _retryable_execd_error(error)
                    or attempt + 1 == attempts
                ):
                    raise
                delay = min(2**attempt, 5)
                self.logger.warning(
                    "YiCloud execd command request failed transiently; "
                    "retrying in %ss (attempt %s/%s): %s",
                    delay,
                    attempt + 1,
                    attempts,
                    error,
                )
                time.sleep(delay)
        else:
            raise AssertionError("unreachable")
        try:
            events = _parse_sse(response_text)
            stdout = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stdout"
            )
            stderr = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stderr"
            )
            matches = re.findall(rf"{re.escape(EXIT_MARKER)}(\d+)", stdout)
            return_code = int(matches[-1]) if matches else 1
            stdout = re.sub(
                rf"\n?{re.escape(EXIT_MARKER)}\d+\n?", "", stdout
            )
            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                return_code=return_code,
            )
        finally:
            self._cleanup_remote_exec_paths_sync(*cleanup_paths)

    def _run_command_detached_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        timeout_sec: int,
        uid: int | None = None,
    ) -> ExecResult:
        command_id = uuid.uuid4().hex
        prefix = f"/tmp/harbor-detached-{command_id}"
        script_path = f"{prefix}.sh"
        stdout_path = f"{prefix}.stdout"
        stderr_path = f"{prefix}.stderr"
        status_path = f"{prefix}.status"
        pid_path = f"{prefix}.pid"
        launch_dir = f"{prefix}.launch"
        cleanup_paths = (
            script_path,
            stdout_path,
            stderr_path,
            status_path,
            f"{status_path}.tmp",
            pid_path,
            f"{pid_path}.tmp",
            launch_dir,
        )
        script = (
            "#!/bin/sh\n"
            "set +e\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(pid_path)}.tmp\n"
            f"mv -f {shlex.quote(pid_path)}.tmp {shlex.quote(pid_path)}\n"
            "(\n"
            f"{command}\n"
            f") > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}\n"
            "harbor_rc=$?\n"
            f"printf '%s\\n' \"$harbor_rc\" > {shlex.quote(status_path)}.tmp\n"
            f"mv -f {shlex.quote(status_path)}.tmp {shlex.quote(status_path)}\n"
            "exit \"$harbor_rc\"\n"
        )
        encoded_script = base64.b64encode(script.encode()).decode()
        launch_command = (
            f"harbor_pid_path={shlex.quote(pid_path)}\n"
            f"harbor_launch_dir={shlex.quote(launch_dir)}\n"
            "harbor_detached_started() {\n"
            f"  [ -f {shlex.quote(status_path)} ] && return 0\n"
            "  [ -s \"$harbor_pid_path\" ] || return 1\n"
            "  harbor_pid=$(cat \"$harbor_pid_path\")\n"
            "  case \"$harbor_pid\" in ''|*[!0-9]*) return 1 ;; esac\n"
            "  kill -0 \"$harbor_pid\" 2>/dev/null\n"
            "}\n"
            "if [ -d \"$harbor_launch_dir\" ] "
            "&& ! harbor_detached_started; then\n"
            "  for harbor_wait in 1 2 3 4 5; do\n"
            "    sleep 1\n"
            "    harbor_detached_started && break\n"
            "  done\n"
            "fi\n"
            "if ! harbor_detached_started; then\n"
            "  rm -rf \"$harbor_launch_dir\"\n"
            "  mkdir \"$harbor_launch_dir\" || exit 75\n"
            f"  printf %s {shlex.quote(encoded_script)} | base64 -d > "
            f"{shlex.quote(script_path)} && chmod 700 {shlex.quote(script_path)} "
            "|| { rm -rf \"$harbor_launch_dir\"; exit 75; }\n"
            "  if command -v bash >/dev/null 2>&1; then\n"
            "    harbor_shell=$(command -v bash)\n"
            "  else\n"
            "    harbor_shell=/bin/sh\n"
            "  fi\n"
            "  if command -v setsid >/dev/null 2>&1; then\n"
            f"    nohup setsid \"$harbor_shell\" {shlex.quote(script_path)} "
            "</dev/null >/dev/null 2>&1 9>&- &\n"
            "  else\n"
            f"    nohup \"$harbor_shell\" {shlex.quote(script_path)} "
            "</dev/null >/dev/null 2>&1 9>&- &\n"
            "  fi\n"
            "  for harbor_wait in 1 2 3 4 5; do\n"
            "    harbor_detached_started && break\n"
            "    sleep 1\n"
            "  done\n"
            "  rm -rf \"$harbor_launch_dir\"\n"
            "  harbor_detached_started || exit 75\n"
            "fi\n"
        )
        launched = self._run_command_direct_sync(
            launch_command,
            cwd,
            env,
            60,
            uid,
        )
        if launched.return_code != 0:
            self._cleanup_remote_exec_paths_sync(*cleanup_paths)
            return launched

        poll_command = (
            f"if [ ! -f {shlex.quote(status_path)} ]; then\n"
            f"  if [ -f {shlex.quote(pid_path)} ] "
            f"&& ! kill -0 \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null; then\n"
            "    printf 'detached command exited without status\\n' >&2\n"
            "    exit 125\n"
            "  fi\n"
            f"  printf %s {shlex.quote(DETACHED_PENDING_MARKER)}\n"
            "  exit 0\n"
            "fi\n"
            f"cat {shlex.quote(stdout_path)} 2>/dev/null\n"
            f"cat {shlex.quote(stderr_path)} >&2 2>/dev/null\n"
            f"harbor_rc=$(cat {shlex.quote(status_path)})\n"
            "exit \"$harbor_rc\"\n"
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            result = self._run_command_direct_sync(
                poll_command,
                "/",
                {},
                60,
                uid,
            )
            if result.stdout != DETACHED_PENDING_MARKER:
                self._cleanup_remote_exec_paths_sync(*cleanup_paths)
                return result
            time.sleep(
                min(
                    EXECD_DETACHED_POLL_INTERVAL_SEC,
                    max(deadline - time.monotonic(), 0),
                )
            )

        terminate_command = (
            f"if [ -s {shlex.quote(pid_path)} ]; then\n"
            f"  harbor_pid=$(cat {shlex.quote(pid_path)})\n"
            "  if kill -TERM -- \"-$harbor_pid\" 2>/dev/null; then\n"
            "    sleep 2\n"
            "    kill -KILL -- \"-$harbor_pid\" 2>/dev/null || true\n"
            "  else\n"
            "    harbor_process_tree=\"$harbor_pid\"\n"
            "    harbor_frontier=\"$harbor_pid\"\n"
            "    while [ -n \"$harbor_frontier\" ]; do\n"
            "      harbor_next=\"\"\n"
            "      for harbor_parent in $harbor_frontier; do\n"
            "        for harbor_status in /proc/[0-9]*/status; do\n"
            "          [ -r \"$harbor_status\" ] || continue\n"
            "          harbor_child=${harbor_status#/proc/}\n"
            "          harbor_child=${harbor_child%/status}\n"
            "          harbor_ppid=\"\"\n"
            "          while IFS=: read -r harbor_key harbor_value; do\n"
            "            if [ \"$harbor_key\" = PPid ]; then\n"
            "              harbor_ppid=\"${harbor_value#\"${harbor_value%%[![:space:]]*}\"}\"\n"
            "              break\n"
            "            fi\n"
            "          done < \"$harbor_status\"\n"
            "          if [ \"$harbor_ppid\" = \"$harbor_parent\" ]; then\n"
            "            harbor_next=\"$harbor_next $harbor_child\"\n"
            "          fi\n"
            "        done\n"
            "      done\n"
            "      [ -n \"$harbor_next\" ] || break\n"
            "      harbor_process_tree=\"$harbor_next $harbor_process_tree\"\n"
            "      harbor_frontier=\"$harbor_next\"\n"
            "    done\n"
            "    for harbor_tree_pid in $harbor_process_tree; do\n"
            "      kill -TERM \"$harbor_tree_pid\" 2>/dev/null || true\n"
            "    done\n"
            "    sleep 2\n"
            "    for harbor_tree_pid in $harbor_process_tree; do\n"
            "      kill -KILL \"$harbor_tree_pid\" 2>/dev/null || true\n"
            "    done\n"
            "  fi\n"
            "fi\n"
        )
        self._run_command_direct_sync(
            terminate_command,
            "/",
            {},
            60,
            uid,
        )
        self._cleanup_remote_exec_paths_sync(*cleanup_paths)
        return ExecResult(
            stdout="",
            stderr=f"command timed out after {timeout_sec}s",
            return_code=124,
        )

    def _run_command_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        timeout_sec: int | None,
        uid: int | None = None,
    ) -> ExecResult:
        if (
            timeout_sec is not None
            and timeout_sec > EXECD_DETACHED_COMMAND_MIN_TIMEOUT_SEC
        ):
            return self._run_command_detached_sync(
                command,
                cwd,
                env,
                timeout_sec,
                uid,
            )
        return self._run_command_direct_sync(
            command,
            cwd,
            env,
            timeout_sec,
            uid,
        )

    def _resolve_exec_uid(self, user: str | int | None) -> int | None:
        if user is None:
            return None
        if isinstance(user, bool):
            raise TypeError(f"invalid exec user: {user!r}")
        if isinstance(user, int):
            if user < 0:
                raise ValueError(f"exec uid must be non-negative: {user}")
            return user
        if user == "root":
            return 0
        if user.isdigit():
            return int(user)
        self.logger.debug(
            "YiCloud execd only supports numeric UIDs (or 'root'); "
            "got username %r, using the sandbox default user",
            user,
        )
        return None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        uid = self._resolve_exec_uid(self._resolve_user(user))
        effective_cwd = cwd or self.task_env_config.workdir
        result = await asyncio.to_thread(
            self._run_command_sync,
            command,
            effective_cwd,
            self._merge_env(env),
            timeout_sec,
            uid,
        )
        callback = self._output_callback()
        if callback is not None:
            if result.stdout:
                await callback(result.stdout, "stdout")
            if result.stderr:
                await callback(result.stderr, "stderr")
        return result

    async def service_exec(
        self,
        service: str | None,
        command: str,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        """Execute on a named sidecar; None and main retain Harbor routing."""
        name = service or self._main_service
        if not self._services or name == self._main_service:
            return await self.exec(command, cwd=cwd, timeout_sec=timeout_sec)
        runtime = self._services.get(name)
        if runtime is None:
            raise ValueError(f"unknown OpenSandbox service: {name!r}")
        return await self._run_service_command(
            runtime, command, cwd=cwd or "/", timeout_sec=timeout_sec or 3600
        )

    async def stop_service(self, service: str) -> None:
        runtime = self._services.get(service)
        if runtime is None:
            raise ValueError(f"unknown OpenSandbox service: {service!r}")
        await self._delete_service(runtime)

    def _uses_s3_upload(self) -> bool:
        return getattr(self, "_upload_backend", "http") == "s3"

    def _s3_store(self) -> S3UploadStore:
        store = getattr(self, "_s3_upload_store", None)
        if store is None:
            raise RuntimeError("YiCloud S3 upload backend is not configured")
        return store

    async def _ensure_s3_downloader(self, signed_url: str) -> None:
        if getattr(self, "_s3_downloader_ready", False):
            return
        lock = getattr(self, "_s3_downloader_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._s3_downloader_lock = lock
        async with lock:
            if getattr(self, "_s3_downloader_ready", False):
                return
            probe = await self.exec(
                "if command -v curl >/dev/null 2>&1 || "
                "command -v wget >/dev/null 2>&1 || "
                "command -v python3 >/dev/null 2>&1; then "
                "printf native; "
                "elif command -v bash >/dev/null 2>&1; then "
                "printf bootstrap; "
                "else printf missing; exit 45; fi",
                cwd="/",
                timeout_sec=30,
                user="root",
            )
            mode = str(getattr(probe, "stdout", "") or "").strip()
            if probe.return_code == 0 and mode == "native":
                self._s3_downloader_ready = True
                return
            if probe.return_code != 0 or mode != "bootstrap":
                raise RuntimeError(
                    "task image cannot download S3 artifacts: install curl, "
                    "wget, python3, or bash"
                )
            if urlsplit(signed_url).scheme != "http":
                raise RuntimeError(
                    "task image only has bash, but the minimal S3 downloader "
                    "requires an HTTP signed URL"
                )
            upload_url = self._fast_upload_url()
            if not upload_url:
                raise RuntimeError(
                    "task image needs the minimal S3 downloader, but "
                    "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN is disabled"
                )
            with tempfile.TemporaryDirectory(
                prefix="yicloud-s3-bootstrap-"
            ) as tmp:
                source = Path(tmp) / "http-get.sh"
                source.write_text(S3_HTTP_BOOTSTRAP, encoding="utf-8")
                await self._upload_file_fast(
                    source,
                    S3_HTTP_BOOTSTRAP_PATH,
                    upload_url,
                    "700",
                )
            prepared = await self.exec(
                f"chmod 700 {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)}",
                cwd="/",
                timeout_sec=30,
                user="root",
            )
            if prepared.return_code != 0:
                raise RuntimeError(
                    "failed to prepare minimal S3 downloader: "
                    f"return_code={prepared.return_code} "
                    f"stderr={getattr(prepared, 'stderr', '')!r}"
                )
            self._s3_downloader_ready = True
            self.logger.info(
                "YiCloud S3 downloader bootstrap uploaded once "
                "path=%s size_bytes=%s",
                S3_HTTP_BOOTSTRAP_PATH,
                len(S3_HTTP_BOOTSTRAP.encode("utf-8")),
            )

    def _s3_download_command(
        self, artifact: S3UploadArtifact, temporary: str
    ) -> str:
        target = shlex.quote(temporary)
        timeout = self._s3_download_timeout_sec
        return (
            f"rm -f {target}; "
            "if command -v curl >/dev/null 2>&1; then "
            f"curl --noproxy '*' --fail --silent --show-error --location "
            f"--retry 3 --connect-timeout 30 --max-time {timeout} "
            f"--output {target} \"$HARBOR_S3_URL\" || exit 48; "
            "elif command -v wget >/dev/null 2>&1; then "
            f"wget --no-proxy -q -O {target} \"$HARBOR_S3_URL\" || exit 48; "
            "elif command -v python3 >/dev/null 2>&1; then "
            "python3 -c 'import shutil,sys,urllib.request; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            "response=opener.open(sys.argv[1]); "
            "output=open(sys.argv[2],\"wb\"); "
            "shutil.copyfileobj(response,output); output.close(); response.close()' "
            f"\"$HARBOR_S3_URL\" {target} || exit 48; "
            f"elif [ -x {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)} ]; then "
            f"bash {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)} {target} || exit 48; "
            "else exit 45; fi; "
            f"actual_size=$(wc -c < {target} | tr -d ' '); "
            f"[ \"$actual_size\" = {artifact.payload_size} ] || "
            f"{{ rm -f {target}; exit 46; }}; "
            f"actual_digest=$(sha256sum {target} | awk '{{print $1}}'); "
            f"[ \"$actual_digest\" = "
            f"{shlex.quote(artifact.payload_digest)} ] || "
            f"{{ rm -f {target}; exit 47; }}"
        )

    async def _materialize_s3_file(
        self,
        artifact: S3UploadArtifact,
        target_path: str,
        mode: str,
    ) -> None:
        await self._ensure_s3_downloader(artifact.signed_url)
        parent = str(Path(target_path).parent)
        temporary = f"{target_path}.harbor-upload-{time.time_ns()}.tmp"
        command = (
            f"mkdir -p {shlex.quote(parent)} && "
            f"({self._s3_download_command(artifact, temporary)}) && "
            f"chmod {mode} {shlex.quote(temporary)} && "
            f"mv -f {shlex.quote(temporary)} {shlex.quote(target_path)}"
        )
        result = await self.exec(
            command,
            cwd="/",
            env={"HARBOR_S3_URL": artifact.signed_url},
            timeout_sec=self._s3_download_timeout_sec + 60,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to materialize S3 file to {target_path!r}: "
                f"return_code={result.return_code} "
                f"stdout={getattr(result, 'stdout', '')!r} "
                f"stderr={getattr(result, 'stderr', '')!r}"
            )

    async def _materialize_s3_directory(
        self, artifact: S3UploadArtifact, target_dir: str
    ) -> None:
        await self._ensure_s3_downloader(artifact.signed_url)
        temporary = f"/tmp/harbor-s3-{time.time_ns()}.tar"
        command = (
            f"({self._s3_download_command(artifact, temporary)}) && "
            f"mkdir -p {shlex.quote(target_dir)} && "
            f"tar xf {shlex.quote(temporary)} "
            f"-C {shlex.quote(target_dir)} && "
            f"rm -f {shlex.quote(temporary)}"
        )
        result = await self.exec(
            command,
            cwd="/",
            env={"HARBOR_S3_URL": artifact.signed_url},
            timeout_sec=self._s3_download_timeout_sec + 300,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to materialize S3 directory to {target_dir!r}: "
                f"return_code={result.return_code} "
                f"stdout={getattr(result, 'stdout', '')!r} "
                f"stderr={getattr(result, 'stderr', '')!r}"
            )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        if not source.is_file():
            raise RuntimeError(f"upload source is not a file: {source}")
        mode = f"{stat.S_IMODE(source.stat().st_mode):o}"
        if self._uses_s3_upload():
            started = time.monotonic()
            artifact = await asyncio.to_thread(
                self._s3_store().stage_file, source
            )
            await self._materialize_s3_file(artifact, target_path, mode)
            elapsed = max(time.monotonic() - started, 0.001)
            self.logger.info(
                "YiCloud upload complete backend=s3 kind=file "
                "target=%s digest=%s size_bytes=%s elapsed_seconds=%.3f",
                target_path,
                artifact.logical_digest,
                artifact.payload_size,
                elapsed,
            )
            return
        parent = str(Path(target_path).parent)
        fast_upload_url = self._fast_upload_url()
        remote_chunk = f"/tmp/harbor-upload-{time.time_ns()}.chunk"
        prepare = await self.exec(
            f"mkdir -p {shlex.quote(parent)} && : > {shlex.quote(target_path)}",
            cwd="/",
            timeout_sec=60,
            user="root",
        )
        if prepare.return_code != 0:
            raise RuntimeError(f"failed to prepare upload target {target_path!r}")
        if fast_upload_url:
            await self._upload_file_fast(
                source,
                target_path,
                fast_upload_url,
                mode,
            )
            return
        handle = None
        try:
            handle = await asyncio.to_thread(source.open, "rb")
            while chunk := await asyncio.to_thread(
                handle.read, UPLOAD_CHUNK_BYTES
            ):
                await asyncio.to_thread(
                    self._upload_chunk_sync,
                    chunk,
                    remote_chunk,
                    source.name,
                )
                append = await self.exec(
                    f"base64 -d {shlex.quote(remote_chunk)} >> "
                    f"{shlex.quote(target_path)} && "
                    f"rm -f {shlex.quote(remote_chunk)}",
                    cwd="/",
                    timeout_sec=120,
                    user="root",
                )
                if append.return_code != 0:
                    raise RuntimeError(
                        f"failed to append upload chunk to {target_path!r}: "
                        f"{append.stderr}"
                    )
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            await self.exec(
                f"rm -f {shlex.quote(remote_chunk)}",
                cwd="/",
                timeout_sec=60,
                user="root",
            )
        finalize = await self.exec(
            f"chmod {mode} {shlex.quote(target_path)}",
            cwd="/",
            timeout_sec=60,
            user="root",
        )
        if finalize.return_code != 0:
            raise RuntimeError(
                f"failed to preserve upload mode for {target_path!r}: "
                f"{finalize.stderr}"
            )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        if not source.is_dir():
            raise RuntimeError(f"upload source is not a directory: {source}")
        if self._uses_s3_upload():
            started = time.monotonic()
            artifact = await asyncio.to_thread(
                self._s3_store().stage_directory, source
            )
            await self._materialize_s3_directory(artifact, target_dir)
            elapsed = max(time.monotonic() - started, 0.001)
            self.logger.info(
                "YiCloud upload complete backend=s3 kind=directory "
                "target=%s digest=%s payload_bytes=%s compression=%s "
                "elapsed_seconds=%.3f",
                target_dir,
                artifact.logical_digest,
                artifact.payload_size,
                artifact.compression,
                elapsed,
            )
            return
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive = Path(tmp_dir) / "upload.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source, arcname=".")
            remote_archive = f"/tmp/harbor-upload-{time.time_ns()}.tar.gz"
            await self.upload_file(archive, remote_archive)
            result = await self.exec(
                f"mkdir -p {shlex.quote(target_dir)} && "
                f"tar xzf {shlex.quote(remote_archive)} -C {shlex.quote(target_dir)} && "
                f"rm -f {shlex.quote(remote_archive)}",
                cwd="/",
                timeout_sec=180,
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError(f"failed to upload directory to {target_dir!r}")

    async def _materialize_read_only_mounts(self) -> None:
        """Copy immutable host bind mounts into a non-mounted Sandbox."""
        for mount in getattr(self, "_mounts", []):
            if mount.get("type") != "bind" or not mount.get("read_only"):
                continue

            source_value = str(mount.get("source") or "").strip()
            target = str(mount.get("target") or "").strip()
            if not source_value or not target:
                raise RuntimeError(
                    "read-only OpenSandbox bind mounts require source and target"
                )
            if not target.startswith("/"):
                raise RuntimeError(
                    f"OpenSandbox mount target must be absolute: {target!r}"
                )

            source = Path(source_value)
            if not source.exists():
                raise RuntimeError(
                    f"OpenSandbox mount source does not exist: {source}"
                )

            self.logger.info(
                "Materializing read-only mount source=%s target=%s",
                source,
                target,
            )
            if source.is_dir():
                await self.upload_dir(source, target)
            elif source.is_file():
                await self.upload_file(source, target)
            else:
                raise RuntimeError(
                    f"unsupported OpenSandbox mount source type: {source}"
                )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        result = await self.exec(
            f"base64 -w0 {shlex.quote(source_path)}",
            cwd="/",
            timeout_sec=180,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(f"failed to download {source_path!r}: {result.stderr}")
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(result.stdout or "", validate=True))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        result = await self.exec(
            f"tar czf - -C {shlex.quote(source_dir)} . | base64 -w0",
            cwd="/",
            timeout_sec=300,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(f"failed to download directory {source_dir!r}")
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive = Path(tmp_dir) / "download.tar.gz"
            archive.write_bytes(base64.b64decode(result.stdout or "", validate=True))
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(target, filter="data")
