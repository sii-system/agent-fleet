"""YiCloud lifecycle: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import asyncio
import re
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from async_utils import configured_threads

from .client import (
    CONTROL_PLANE_AUTH_ATTEMPTS,
    SANDBOX_STATUS_REQUEST_ATTEMPTS,
    TERMINAL_FAILURE_STATES,
    YiCloudSandboxReadyTimeoutError,
    _access_token_of,
    _command_url_of,
    _state_of,
    _status_reason_of,
    _validate_sandbox_binding,
)
from .services import ServiceRuntime

_monotonic = time.monotonic


_AUXILIARY_LOOPS = weakref.WeakSet()
_SANDBOX_LISTINGS = weakref.WeakKeyDictionary()


class _SandboxListing:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0
        self.started_at = 0.0
        self.ids: set[str] | None = None


def _configure_auxiliary_pool() -> None:
    loop = asyncio.get_running_loop()
    if loop in _AUXILIARY_LOOPS:
        return
    workers = configured_threads("HARBOR_OPENSANDBOX_AUX_THREADS", None)
    if workers is not None:
        loop.set_default_executor(
            ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="sandbox-aux",
            )
        )
    _AUXILIARY_LOOPS.add(loop)


class YiCloudLifecycleMixin:
    """Internal lifecycle methods sharing the adapter instance state."""

    def _make_sandbox_name(self) -> str:
        raw = f"harbor-{self.session_id}".lower()
        normalized = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
        return (normalized or "harbor-trial")[:63].rstrip("-")

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
                if consecutive_status_errors >= SANDBOX_STATUS_REQUEST_ATTEMPTS:
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
        await self._close_async_commands()
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

    async def _wait_for_sandbox_ids_absent(self, sandbox_ids: set[str]) -> bool:
        """Return only after the control-plane list no longer contains IDs."""
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_ids:
            return True
        timeout = asyncio.timeout(self._cleanup_wait_sec)
        try:
            async with timeout:
                while True:
                    remaining = await self._listed_sandbox_ids(sandbox_ids)
                    if not remaining:
                        return True
                    await asyncio.sleep(3)
        except TimeoutError:
            if not timeout.expired():
                raise
            return False

    async def _listed_sandbox_ids(self, sandbox_ids: set[str]) -> set[str]:
        sandbox = self._sandbox_service
        if sandbox is None or not sandbox_ids:
            return set()
        loop = asyncio.get_running_loop()
        requested_at = loop.time()
        listings = _SANDBOX_LISTINGS.setdefault(loop, {})
        key = (id(sandbox), self._project_name, self._environment_id)
        listing = listings.setdefault(key, _SandboxListing())
        listing.users += 1
        try:
            async with listing.lock:
                if listing.ids is None or listing.started_at < requested_at:
                    listing.ids = None
                    # Admit callers already queued on this loop before scanning.
                    await asyncio.sleep(0)
                    listing.started_at = loop.time()
                    listing.ids = await self._scan_sandbox_ids()
                return sandbox_ids & listing.ids
        finally:
            listing.users -= 1
            if not listing.users:
                del listings[key]
                if not listings:
                    del _SANDBOX_LISTINGS[loop]

    async def _scan_sandbox_ids(self) -> set[str]:
        sandbox = self._sandbox_service
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

    async def start(self, force_build: bool) -> None:
        _configure_auxiliary_pool()
        self._async_commands_stopping = False
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
                await self._close_async_commands()
                await self._delete_service_group()
                raise
            return
        self._sandbox_name = self._make_sandbox_name()

        ready_started_at = _monotonic()
        self._create_request_started_at = ready_started_at
        created = await self._retry_control_plane_auth(
            "create sandbox",
            sandbox.create_sandbox,
            None,
            sandbox.models.CreateSandboxReq(
                ProjectName=self._project_name,
                EnvironmentId=self._environment_id,
                Name=self._sandbox_name,
                Image=self._create_image_input(sandbox, self._image_source_ref),
                Entrypoint=["sleep", "infinity"],
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
            running_observed_at = _monotonic()
            running_elapsed_ms = (running_observed_at - ready_started_at) * 1000
            _validate_sandbox_binding(current, self._environment_id, self._image_ref)
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
            ready_observed_at = _monotonic()
            self._ready_observed_at = ready_observed_at
            ready_elapsed_ms = (ready_observed_at - ready_started_at) * 1000
            self.logger.info(
                "YiCloud Sandbox ready sandbox_id=%s "
                "running_elapsed_ms=%.3f ready_elapsed_ms=%.3f",
                self._sandbox_id,
                running_elapsed_ms,
                ready_elapsed_ms,
            )
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
            initialized_at = _monotonic()
            self._initialized_at = initialized_at
            self.logger.info(
                "YiCloud Sandbox initialized sandbox_id=%s "
                "ready_to_initialized_ms=%.3f create_to_initialized_ms=%.3f",
                self._sandbox_id,
                (initialized_at - ready_observed_at) * 1000,
                (initialized_at - ready_started_at) * 1000,
            )
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
        try:
            await self._close_async_commands()
        finally:
            await self._stop_sandbox(delete)

    async def _close_async_commands(self) -> None:
        self._async_commands_stopping = True
        runner = getattr(self, "_async_command_runner", None)
        if runner is not None:
            await runner.close()
            self._async_command_runner = None

    async def _stop_sandbox(self, delete: bool) -> None:
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
