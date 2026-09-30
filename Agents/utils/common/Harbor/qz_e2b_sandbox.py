"""qz (SII Inspire) sandbox provider adapter for Harbor's E2B environment.

The qz sandbox service (``https://qz-sbx-api.sii.edu.cn``) is a self-hosted
E2B-compatible control plane: the REST surface is E2B's (``NewSandbox`` with
``templateID``, ``X-API-Key`` auth, template aliases) mounted under a ``/v1``
prefix, and the platform's own ``inspire_sandbox`` SDK is a rebranded E2B
Python SDK driven by ``SBX_API_KEY`` / ``SBX_API_URL``. Harbor 0.18 already
ships an E2B environment backed by the official ``e2b`` SDK, which supports
self-hosted deployments through ``E2B_API_URL`` / ``E2B_SANDBOX_URL``
overrides, so this adapter only maps qz configuration onto the official SDK
and reuses the complete E2B environment implementation.

Verified end to end against the live service (2026-08-13, template
``agent_fleet_probe``): create, get_info, exec with cwd/env/user overrides,
file write/read round-trip, and kill all pass through the official SDK with
the mappings below. A later capacity probe verified 100 simultaneously active
Sandboxes when create requests used the platform-confirmed rolling window of
10. The qz-specific deltas:

- ``POST /v1/sandboxes`` accepts E2B ``NewSandbox`` bodies; ``templateID`` is
  required. Templates are registered on the platform (image + spec + key
  binding); the e2b ``/v2/templates`` build flow is not mounted.
- The auth header is ``X-API-Key``. Keys use an ``sbx_`` prefix, so the SDK's
  default ``e2b_`` prefix validation must stay disabled.
- The official ``e2b`` SDK reaches the control plane once ``E2B_API_URL``
  includes the ``/v1`` prefix; ``SBX_API_URL`` follows the platform docs and
  stays prefix-free, so the mapping below appends it.
- The envd data plane host is ``sbx-{port}-{sandbox_id}.{sandbox_domain}``
  (the create response's ``hostTemplate``). The SDK builds the host without
  the ``sbx-`` prefix and does not consume ``hostTemplate``; the unprefixed
  host lands on the platform gateway's own auth (401), so ``get_host`` is
  patched here.
- One current QZ type group has a single node that accepts at most 10
  simultaneous create requests and rejects excess requests instead of queuing
  them. A runner-wide file-backed rolling window shapes create traffic without
  limiting the number of already-active Sandboxes. Operators can raise
  ``QZ_CREATE_CONCURRENCY`` after QZ confirms additional create capacity;
  benchmark users only select their normal worker count.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.e2b import E2BEnvironment
from qz_create_limiter import qz_create_concurrency, qz_create_slot
from qz_template_resolver import (
    MAPPING_ENV_VAR,
    QzTemplateResolutionError,
    load_mapping,
    resolve_task_environment_from_environment,
)

try:
    import httpcore
    import httpx
    from e2b.exceptions import RateLimitException
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    # Sandbox creation is not idempotent: once the request reaches the
    # control plane, a lost response under a blanket retry would allocate a
    # second sandbox and orphan the first for its full lifetime (up to the
    # 4-hour cap). Retry only failures that prove the request never got
    # there -- connection establishment and 429s -- mirroring the
    # dispatch-retry rationale in Harbor's E2B backend. Post-dispatch
    # failures defer to Harbor's trial-level retry, with the platform
    # lifetime as the orphan backstop.
    _CREATE_RETRYABLE: tuple[type[BaseException], ...] = (
        httpcore.ConnectError,
        httpcore.ConnectTimeout,
        httpcore.PoolTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        RateLimitException,
    )
    _retry_create = retry(
        retry=retry_if_exception_type(_CREATE_RETRYABLE),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
except ImportError:  # unit tests run without the harbor/e2b dependency set

    def _retry_create(fn):
        return fn


QZ_DEFAULT_API_URL = "https://qz-sbx-api.sii.edu.cn"
QZ_DEFAULT_HOST_PREFIX = "sbx-"
# The platform rejects sandbox timeouts above 4 hours ("Timeout cannot be
# greater than 4 hours"), while Harbor's E2B backend hardcodes 24h.
QZ_MAX_SANDBOX_TIMEOUT_SEC = 4 * 60 * 60
QZ_DEFAULT_SANDBOX_TIMEOUT_SEC = QZ_MAX_SANDBOX_TIMEOUT_SEC
QZ_TASK_INIT_TIMEOUT_SEC = 600
_API_VERSION_SUFFIX = "/v1"
# These settings describe a particular cloud-E2B data plane rather than the
# shared SDK API surface. Letting them leak into a qz process can create the
# sandbox through qz and then send commands or files to the other provider.
_AMBIENT_E2B_TRANSPORT_VARS = (
    "E2B_SANDBOX_URL",
    "E2B_DOMAIN",
    "E2B_DEBUG",
    "E2B_ACCESS_TOKEN",
)


def qz_sandbox_timeout_sec() -> int:
    """Resolve and validate QZ_SANDBOX_TIMEOUT_SEC.

    Rejects bad values up front: a non-numeric or out-of-range setting would
    otherwise surface deep inside trial startup (ValueError) or as retried
    platform 400s.
    """
    raw = os.environ.get("QZ_SANDBOX_TIMEOUT_SEC", "").strip()
    if not raw:
        return QZ_DEFAULT_SANDBOX_TIMEOUT_SEC
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"QZ_SANDBOX_TIMEOUT_SEC must be an integer, got {raw!r}"
        ) from None
    if not 1 <= value <= QZ_MAX_SANDBOX_TIMEOUT_SEC:
        raise ValueError(
            "QZ_SANDBOX_TIMEOUT_SEC must be between 1 and "
            f"{QZ_MAX_SANDBOX_TIMEOUT_SEC} (the platform's 4-hour cap), "
            f"got {value}"
        )
    return value


_TEMPLATE_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9_]")


def sanitize_template_name(name: str) -> str:
    """Fold a Harbor template alias onto qz's allowed alphabet.

    qz template names accept only letters, digits, and underscores, while
    Harbor's auto-generated aliases ({task}__{hash}) routinely contain
    hyphens.
    """
    return _TEMPLATE_NAME_ALLOWED.sub("_", name)


def _qz_api_key() -> str:
    explicit_key = (
        os.environ.get("QZ_SANDBOX_API_KEY", "").strip()
        or os.environ.get("SBX_API_KEY", "").strip()
    )
    if explicit_key:
        return explicit_key

    # Preserve the legacy SDK-shaped configuration only when the value is
    # unambiguously a qz key. On a mixed-provider host an e2b_-prefixed key
    # belongs to cloud E2B; carrying it across while rewriting E2B_API_URL to
    # qz would disclose the credential to the wrong control plane.
    legacy_key = os.environ.get("E2B_API_KEY", "").strip()
    return legacy_key if legacy_key.startswith("sbx_") else ""


def _qz_api_url() -> str:
    return (
        os.environ.get("QZ_SANDBOX_API_URL", "").strip()
        or os.environ.get("SBX_API_URL", "").strip()
        or QZ_DEFAULT_API_URL
    )


def _e2b_api_url(qz_url: str) -> str:
    base = qz_url.rstrip("/")
    if base.endswith(_API_VERSION_SUFFIX):
        return base
    return base + _API_VERSION_SUFFIX


def apply_qz_environment() -> None:
    """Point the official e2b SDK's environment at the qz service.

    qz-flavored values take precedence: on a mixed rollout host the ambient
    ``E2B_*`` variables belong to the cloud-E2B backend, and a qz process
    must not defer to them or its requests would go to the wrong control
    plane. ``E2B_API_KEY`` only serves as a fallback when no qz key is
    configured and it contains an unambiguous ``sbx_`` platform key. Empty
    strings count as unset: env.sh exports empty placeholders so the variables
    survive into worker panes.
    """
    for name in _AMBIENT_E2B_TRANSPORT_VARS:
        os.environ.pop(name, None)

    key = _qz_api_key()
    if key:
        os.environ["E2B_API_KEY"] = key
    else:
        os.environ.pop("E2B_API_KEY", None)
    os.environ["E2B_API_URL"] = _e2b_api_url(_qz_api_url())
    # qz keys use the sbx_ prefix, which the SDK's e2b_ prefix validation
    # would reject client-side before any request is sent; an inherited
    # E2B_VALIDATE_API_KEY=true from the cloud backend must not apply here.
    os.environ["E2B_VALIDATE_API_KEY"] = "false"


def patch_envd_host() -> None:
    """Point the SDK's envd host at qz's ``sbx-``-prefixed route.

    ``apply_qz_environment`` removes cloud-E2B transport overrides before a
    config is constructed, so the SDK reaches this host resolver. Idempotent;
    a no-op when the e2b extra is missing (E2BEnvironment's own import guard
    reports that case).
    """
    try:
        from e2b.connection_config import ConnectionConfig
    except ImportError:
        return
    if getattr(ConnectionConfig, "_qz_host_patched", False):
        return

    def get_host(self, sandbox_id: str, sandbox_domain: str, port: int) -> str:
        prefix = os.environ.get("QZ_SANDBOX_HOST_PREFIX") or QZ_DEFAULT_HOST_PREFIX
        return f"{prefix}{port}-{sandbox_id}.{sandbox_domain or self.domain}"

    ConnectionConfig.get_host = get_host
    ConnectionConfig._qz_host_patched = True


class QzSandboxEnvironment(E2BEnvironment):
    """Run one Harbor task in a qz (SII Inspire) managed E2B sandbox.

    Templates must be pre-registered on the platform (image + spec + key
    binding); qz does not mount the e2b remote build API. An explicit
    ``template`` kwarg or ``QZ_SANDBOX_TEMPLATE`` selects one fixed Template.
    Alternatively, ``QZ_SANDBOX_TEMPLATE_MAP`` resolves each task through a
    mapping and verifies that the selected Template is live and ready. A
    environment mapping may also provide commands to initialize each fresh
    Sandbox before Harbor starts the agent. Without either mode, Harbor's
    auto-generated per-task alias is folded onto qz's allowed name alphabet.
    """

    def __init__(self, *args, template: str | None = None, **kwargs) -> None:
        apply_qz_environment()
        patch_envd_host()
        fixed_template = (
            template
            if template is not None
            else os.environ.get("QZ_SANDBOX_TEMPLATE", "")
        ).strip()
        mapping_path = os.environ.get(MAPPING_ENV_VAR, "").strip()
        if fixed_template and mapping_path:
            raise ValueError(
                f"set only one of QZ_SANDBOX_TEMPLATE or {MAPPING_ENV_VAR}, not both"
            )
        self._template_override = fixed_template
        self._task_init_steps: tuple[dict[str, str], ...] = ()
        super().__init__(*args, **kwargs)
        if self._template_override:
            self._template_name = self._template_override
        elif mapping_path:
            try:
                environment_plan = resolve_task_environment_from_environment(
                    Path(mapping_path),
                    self.environment_name,
                )
            except (OSError, QzTemplateResolutionError) as exc:
                raise RuntimeError(
                    "failed to resolve QZ Template for task "
                    f"{self.environment_name!r}: {exc}"
                ) from exc
            self._template_override = environment_plan.template_id
            self._task_init_steps = environment_plan.init_steps
            if environment_plan.workdir is not None:
                self._workdir = Path(environment_plan.workdir)
                self.task_env_config.workdir = environment_plan.workdir
            self._template_name = self._template_override
        else:
            self._template_name = sanitize_template_name(self._template_name)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # Advertise only verified behavior. Network isolation (no-network /
        # allowlist) is inherited from the E2B backend but has not been
        # verified on qz, so declaring it unsupported rejects such tasks up
        # front instead of running them unenforced. GPUs and host bind
        # mounts are unavailable.
        return EnvironmentCapabilities(
            gpus=False, disable_internet=False, mounted=False
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # The compute spec is fixed on the platform template and cannot be
        # applied per task at create time. Declaring no support turns an
        # explicit cpu/memory enforcement policy into a fail-fast reject,
        # while the default AUTO policy keeps running with the template's
        # spec.
        return EnvironmentResourceCapabilities()

    async def start(self, force_build: bool) -> None:
        if force_build:
            raise RuntimeError(
                "qz sandbox cannot rebuild templates: they are registered on "
                "the platform, not built by Harbor. Re-register the template "
                "and retry without force_build."
            )
        await super().start(False)

    async def _does_template_exist(self) -> bool:
        # An explicit override may be a template alias or a template ID. The
        # alias lookup can't see IDs, so trust the override and let sandbox
        # creation be the authority -- the platform returns a clear
        # "template 'xxx' not found" for a bad value, which beats failing the
        # pre-check and misreporting "register the template first".
        if self._template_override:
            return True
        return await super()._does_template_exist()

    async def _create_template(self) -> None:
        raise RuntimeError(
            "qz sandbox does not expose the e2b template build API. Register "
            f"template {self._template_name!r} on the platform first (image + "
            "spec + sandbox key binding), or point QZ_SANDBOX_TEMPLATE at an "
            "existing template."
        )

    @_retry_create
    async def _allocate_sandbox(self):
        # Mirrors E2BEnvironment._create_sandbox with the sandbox timeout made
        # configurable: the platform caps it at 4 hours while the base class
        # hardcodes 24h.
        from e2b import AsyncSandbox
        from harbor.models.task.config import NetworkMode

        timeout_sec = qz_sandbox_timeout_sec()
        # Pass the qz connection explicitly so sandbox creation is immune to
        # ambient E2B_* values on a mixed-provider host.
        api_url = os.environ.get("E2B_API_URL", "")
        async with qz_create_slot(api_url):
            return await AsyncSandbox.create(
                template=self._template_name,
                api_key=os.environ.get("E2B_API_KEY") or None,
                api_url=api_url or None,
                metadata={
                    "environment_name": self.environment_name,
                    "session_id": self.session_id,
                },
                timeout=timeout_sec,
                allow_internet_access=(
                    self.network_policy.network_mode != NetworkMode.NO_NETWORK
                ),
                network=self._sandbox_create_network_options(),
            )

    async def _create_sandbox(self) -> None:
        self._sandbox = await self._allocate_sandbox()

        # Platform templates may be generic images that do not contain
        # Harbor's task workdir. Prepare it after allocation, outside the
        # allocation retry boundary: a transport failure here must not create
        # a second sandbox and orphan the first one.
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

        for index, step in enumerate(self._task_init_steps, start=1):
            result = await self._sandbox.commands.run(
                step["run"],
                cwd=step.get("cwd") or str(self._workdir),
                timeout=QZ_TASK_INIT_TIMEOUT_SEC,
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"QZ task init step {index} failed for "
                    f"{self.environment_name!r}: exit {result.exit_code}: "
                    f"{result.stderr or result.stdout}"
                )

    @classmethod
    def preflight(cls) -> None:
        apply_qz_environment()
        patch_envd_host()
        if not os.environ.get("E2B_API_KEY"):
            raise SystemExit(
                "qz sandbox requires an API key: set SBX_API_KEY (or "
                "QZ_SANDBOX_API_KEY / an sbx_-prefixed E2B_API_KEY) before "
                "starting the runner."
            )
        fixed_template = os.environ.get("QZ_SANDBOX_TEMPLATE", "").strip()
        mapping_path = os.environ.get(MAPPING_ENV_VAR, "").strip()
        if fixed_template and mapping_path:
            raise SystemExit(
                f"set only one of QZ_SANDBOX_TEMPLATE or {MAPPING_ENV_VAR}, not both"
            )
        if mapping_path:
            try:
                load_mapping(Path(mapping_path))
            except (OSError, QzTemplateResolutionError) as exc:
                raise SystemExit(
                    f"invalid {MAPPING_ENV_VAR} {mapping_path!r}: {exc}"
                ) from exc
        try:
            qz_sandbox_timeout_sec()
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        try:
            qz_create_concurrency()
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
