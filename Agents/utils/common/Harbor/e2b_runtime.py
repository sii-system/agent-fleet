"""Small runtime compatibility layer for Harbor's E2B environment."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import shlex
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

_TRUE_VALUES = {"1", "true", "yes", "on"}
_CONCURRENT_BUILD_MESSAGES = (
    "build is not in waiting state",
    "build was cancelled",
)
_MISSING_DEFAULT_TAG_MESSAGE = "tag 'default' does not exist"
_VERIFIER_TOOL_NAMES = ("uv", "uvx", "curl")


def _is_true(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE_VALUES


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _template_lock_dir() -> Path:
    configured = os.environ.get("HARBOR_E2B_TEMPLATE_LOCK_DIR", "").strip()
    if configured:
        return Path(configured)

    output_path = os.environ.get("OUTPUT_PATH", "").strip()
    if output_path:
        return Path(output_path) / "runtime" / "e2b-template-locks"

    runtime_dir = os.environ.get("RUNTIME_DIR", "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "e2b-template-locks"

    return Path("/tmp/sii-agent-fleet-e2b-template-locks")


def _template_lock_path(alias: str) -> Path:
    digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()
    return _template_lock_dir() / f"{digest}.lock"


@asynccontextmanager
async def _exclusive_file_lock(path: Path, timeout_sec: float, poll_sec: float):
    """Acquire a Linux flock without blocking the Harbor asyncio event loop."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    deadline = time.monotonic() + timeout_sec
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for E2B template lock: {path}")
                await asyncio.sleep(min(poll_sec, remaining))
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _tag_name(tag: object) -> str:
    if isinstance(tag, dict):
        return str(tag.get("tag", ""))
    return str(getattr(tag, "tag", ""))


async def _default_tag_ready(async_template: Any, alias: str) -> bool:
    if not await async_template.alias_exists(alias):
        return False
    try:
        tags = await async_template.get_tags(alias)
    except Exception as exc:
        # E2B can expose the template record before its first tag is published.
        # Treat only that not-found window as not ready; transport/auth failures
        # must remain visible to the caller.
        if "404" in str(exc):
            return False
        raise
    return any(_tag_name(tag) == "default" for tag in tags)


async def _wait_for_default_tag(
    async_template: Any,
    alias: str,
    *,
    timeout_sec: float,
    poll_sec: float,
) -> bool:
    deadline = time.monotonic() + timeout_sec
    while True:
        if await _default_tag_ready(async_template, alias):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_sec, remaining))


def _is_concurrent_build_error(exc: BaseException) -> bool:
    if exc.__class__.__name__ != "BuildException":
        return False
    message = str(exc).lower()
    return any(fragment in message for fragment in _CONCURRENT_BUILD_MESSAGES)


def _is_missing_default_tag_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "404" in message and _MISSING_DEFAULT_TAG_MESSAGE in message


def patch_e2b_sandbox_timeout_from_env() -> bool:
    """Cap Harbor's requested E2B sandbox lifetime when explicitly configured.

    This is an opt-in compatibility workaround for Harbor 0.18.0, which
    requests a fixed 24-hour lifetime even when an E2B-compatible deployment
    enforces a lower maximum. An empty setting deliberately leaves upstream
    behavior unchanged, so each deployment can choose its own policy.

    TODO: Remove this monkey patch once Harbor exposes sandbox lifetime through
    its public environment or trial configuration.
    """

    # The cap is E2B-specific: on a mixed rollout host, qz workers inherit
    # HARBOR_E2B_SANDBOX_TIMEOUT_SEC too, and the qz adapter manages its own
    # QZ_SANDBOX_TIMEOUT_SEC against the platform's 4-hour maximum.
    if os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower() != "e2b":
        return False

    raw_timeout = os.environ.get("HARBOR_E2B_SANDBOX_TIMEOUT_SEC", "").strip()
    if not raw_timeout:
        return False
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise RuntimeError("HARBOR_E2B_SANDBOX_TIMEOUT_SEC must be a positive integer") from exc
    if timeout <= 0:
        raise RuntimeError("HARBOR_E2B_SANDBOX_TIMEOUT_SEC must be a positive integer")

    from e2b import AsyncSandbox

    if getattr(AsyncSandbox, "_fleet_timeout_patch_applied", False):
        return False

    original_create = AsyncSandbox.create

    async def create_with_timeout_cap(*args: Any, **kwargs: Any):
        requested = kwargs.get("timeout")
        if requested is None or requested > timeout:
            kwargs["timeout"] = timeout
        return await original_create(*args, **kwargs)

    AsyncSandbox.create = create_with_timeout_cap
    AsyncSandbox._fleet_timeout_patch_applied = True
    return True


def patch_e2b_template_coordination_from_env() -> bool:
    """Coordinate cold E2B template builds across Harbor trials.

    Harbor 0.18.0 checks whether an alias exists independently in every trial.
    Concurrent trials can therefore build the same cold alias at once, while
    E2B accepts only one effective build. This compatibility patch keeps one
    local builder per alias, waits for the remote ``default`` tag, and lets all
    trials create their own sandboxes once the template is ready.

    The file lock coordinates processes sharing the runner's writable output
    directory. The remote readiness/error recovery path also allows a loser of
    a build race with another runner to converge on the winning build.

    TODO: Remove this monkey patch once the pinned Harbor version coordinates
    E2B template builds and tag publication itself.
    """

    if os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower() != "e2b":
        return False
    if not _is_true(os.environ.get("HARBOR_E2B_TEMPLATE_COORDINATION"), default=True):
        return False

    ready_timeout_sec = _positive_float_from_env(
        "HARBOR_E2B_TEMPLATE_READY_TIMEOUT_SEC", 600.0
    )
    poll_sec = _positive_float_from_env("HARBOR_E2B_TEMPLATE_POLL_INTERVAL_SEC", 1.0)

    from e2b import AsyncTemplate
    from harbor.environments.e2b import E2BEnvironment

    if getattr(E2BEnvironment, "_fleet_template_coordination_patch_applied", False):
        return False

    original_start = E2BEnvironment.start
    original_create_template = E2BEnvironment._create_template
    original_create_sandbox = E2BEnvironment._create_sandbox

    async def coordinated_template_exists(self) -> bool:  # type: ignore[no-untyped-def]
        return await _default_tag_ready(AsyncTemplate, self._template_name)

    async def coordinated_create_template(self):  # type: ignore[no-untyped-def]
        alias = self._template_name
        force_build = bool(getattr(self, "_fleet_force_build", False))
        lock_path = _template_lock_path(alias)

        async with _exclusive_file_lock(lock_path, ready_timeout_sec, poll_sec):
            # Every waiter rechecks inside the lock. AsyncTemplate.build() waits
            # for build completion, so a successful local builder publishes the
            # tag before releasing this lock.
            if not force_build and await _default_tag_ready(AsyncTemplate, alias):
                self.logger.debug(
                    "E2B template became ready while waiting for build lock: %s",
                    alias,
                )
                return None

            self.logger.debug("Building E2B template as lock owner: %s", alias)
            try:
                result = await original_create_template(self)
            except Exception as exc:
                if not _is_concurrent_build_error(exc):
                    raise
                self.logger.debug(
                    "E2B template build race for %s; waiting for winning build",
                    alias,
                )
                if await _wait_for_default_tag(
                    AsyncTemplate,
                    alias,
                    timeout_sec=ready_timeout_sec,
                    poll_sec=poll_sec,
                ):
                    return None
                raise

            if not await _wait_for_default_tag(
                AsyncTemplate,
                alias,
                timeout_sec=ready_timeout_sec,
                poll_sec=poll_sec,
            ):
                raise TimeoutError(
                    f"E2B template build completed but default tag was not ready: {alias}"
                )
            return result

    async def coordinated_create_sandbox(self):  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + ready_timeout_sec
        while True:
            try:
                return await original_create_sandbox(self)
            except Exception as exc:
                if not _is_missing_default_tag_error(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                self.logger.debug(
                    "E2B default tag not visible for %s; retrying sandbox create",
                    self._template_name,
                )
                await asyncio.sleep(min(poll_sec, remaining))

    async def coordinated_start(self, force_build: bool):  # type: ignore[no-untyped-def]
        self._fleet_force_build = bool(force_build)
        try:
            return await original_start(self, force_build)
        finally:
            self.__dict__.pop("_fleet_force_build", None)

    E2BEnvironment._does_template_exist = coordinated_template_exists
    E2BEnvironment._create_template = coordinated_create_template
    E2BEnvironment._create_sandbox = coordinated_create_sandbox
    E2BEnvironment.start = coordinated_start
    E2BEnvironment._fleet_template_coordination_patch_applied = True
    return True


def patch_e2b_verifier_tools_from_env() -> bool:
    """Upload the runner's offline verifier tools after an E2B sandbox starts.

    E2B has no host bind mounts. The runner therefore prepares the same
    ``uv``/``uvx`` binaries and narrow uv-installer curl shim used by the
    Docker and OpenSandbox backends, then this patch copies them through
    Harbor's E2B file API once the sandbox exists. This keeps task Dockerfiles
    and E2B template construction unchanged.
    """

    environment_type = os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower()
    if environment_type not in {"e2b", "qz"}:
        return False

    raw_source = os.environ.get("HARBOR_E2B_VERIFIER_UV_SOURCE", "").strip()
    if not raw_source:
        return False
    source = Path(raw_source)
    missing = [name for name in _VERIFIER_TOOL_NAMES if not (source / name).is_file()]
    if missing:
        raise RuntimeError(
            "HARBOR_E2B_VERIFIER_UV_SOURCE is missing required files: "
            + ", ".join(missing)
        )

    target = os.environ.get(
        "HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH", "/opt/tb-uv-backup/bin"
    ).strip()
    if not target.startswith("/"):
        raise RuntimeError("HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH must be absolute")

    from harbor.environments.e2b import E2BEnvironment

    if getattr(E2BEnvironment, "_fleet_verifier_tools_patch_applied", False):
        return False

    original_start = E2BEnvironment.start
    quoted_target = shlex.quote(target)
    quoted_tools = " ".join(
        shlex.quote(f"{target.rstrip('/')}/{name}") for name in _VERIFIER_TOOL_NAMES
    )

    async def start_with_verifier_tools(self, force_build: bool):  # type: ignore[no-untyped-def]
        result = await original_start(self, force_build)

        mkdir_result = await self.exec(
            f"mkdir -p -- {quoted_target}",
            user="root",
        )
        if mkdir_result.return_code != 0:
            raise RuntimeError(
                "failed to create E2B verifier tool directory: "
                f"{mkdir_result.stderr or mkdir_result.stdout}"
            )

        await self.upload_dir(source, target)
        chmod_result = await self.exec(
            f"chmod 0755 -- {quoted_tools}",
            user="root",
        )
        if chmod_result.return_code != 0:
            raise RuntimeError(
                "failed to make uploaded E2B verifier tools executable: "
                f"{chmod_result.stderr or chmod_result.stdout}"
            )
        return result

    E2BEnvironment.start = start_with_verifier_tools
    E2BEnvironment._fleet_verifier_tools_patch_applied = True
    return True


def patch_e2b_runtime_from_env() -> bool:
    """Apply all configured E2B compatibility patches."""

    timeout_patched = patch_e2b_sandbox_timeout_from_env()
    coordination_patched = patch_e2b_template_coordination_from_env()
    verifier_tools_patched = patch_e2b_verifier_tools_from_env()
    return timeout_patched or coordination_patched or verifier_tools_patched
