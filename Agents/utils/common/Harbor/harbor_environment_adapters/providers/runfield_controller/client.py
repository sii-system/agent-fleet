"""Access Runfield Controller through the public sii-runfield async SDK.

Infrastructure implementation details remain behind the Controller and optional
client-host UDS accelerator proxy (sandbox-client-proxy). Importing this module
or instantiating RunfieldControllerClient never imports runfield or opens
sockets, keeping portable CI and preflight checks SDK-free.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import weakref
from typing import Any
from urllib.parse import urlsplit

from .archive_client import MAX_ARCHIVE_BYTES, RunfieldArchiveClientMixin

API_VERSION = "sandbox-lifecycle/v1alpha1"
MAX_JSON_RESPONSE_BYTES = 16 << 20
MAX_OUTPUT_READ_BYTES = 1 << 20
_INACTIVE_HANDLE_STATES = {"closed", "create_failed"}


def _load_runfield_sdk() -> tuple[Any, Any, Any, type[Exception]]:
    """Lazily import sii-runfield only when executing a Controller operation."""
    from runfield import ExecutionResult, Sandbox, SandboxClient, SandboxError

    return ExecutionResult, Sandbox, SandboxClient, SandboxError


class _LoopSharedState:
    """Hold one shared SandboxClient and active Sandbox handle table per loop key."""

    def __init__(self, sdk_client: Any) -> None:
        self.sdk_client = sdk_client
        self.sandboxes: dict[str, Any] = {}


_LOOP_SHARED_STATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[Any, ...], _LoopSharedState]
] = weakref.WeakKeyDictionary()


class RunfieldControllerError(RuntimeError):
    """Stable Controller protocol error without request bodies or execution output."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        sandbox_id: str | None = None,
        sandbox_state: str | None = None,
    ) -> None:
        super().__init__(f"Runfield Controller request failed: {code}: {message}")
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.sandbox_id = sandbox_id
        self.sandbox_state = sandbox_state


class RunfieldControllerClient(RunfieldArchiveClientMixin):
    """Own Controller origin validation and delegate transport to shared sii-runfield."""

    def __init__(
        self,
        endpoint: str,
        *,
        allow_insecure_http: bool = False,
        request_timeout_sec: int = 15,
        transfer_timeout_sec: int = 1800,
        proxy_socket: str | None = None,
        proxy_mode: str | None = None,
    ) -> None:
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or endpoint.strip() != endpoint
            or "?" in endpoint
            or "#" in endpoint
            or any(ch in endpoint for ch in ("\x00", "\r", "\n"))
        ):
            raise ValueError("RUNFIELD_CONTROLLER_ENDPOINT must be an HTTP(S) origin")
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                "RUNFIELD_CONTROLLER_ENDPOINT must be an HTTP(S) origin"
            ) from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.netloc
            or parsed.netloc.endswith(":")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("RUNFIELD_CONTROLLER_ENDPOINT must be an HTTP(S) origin")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ValueError(
                "plain HTTP requires RUNFIELD_CONTROLLER_ALLOW_INSECURE_HTTP=1"
            )
        if isinstance(request_timeout_sec, bool) or not 1 <= request_timeout_sec <= 60:
            raise ValueError("request_timeout_sec must be between 1 and 60")
        if isinstance(transfer_timeout_sec, bool) or transfer_timeout_sec <= 0:
            raise ValueError("transfer_timeout_sec must be positive")
        self.endpoint = f"{parsed.scheme}://{parsed.netloc}"
        self.allow_insecure_http = bool(allow_insecure_http)
        self.request_timeout_sec = request_timeout_sec
        self.transfer_timeout_sec = transfer_timeout_sec
        self._explicit_proxy_socket = proxy_socket
        self._explicit_proxy_mode = proxy_mode
        self._sandboxes: dict[str, Any] = {}

    @staticmethod
    def _load_sdk_error() -> type[Exception]:
        _, _, _, sdk_error_cls = _load_runfield_sdk()
        return sdk_error_cls

    @classmethod
    def _extract_error_sandbox(
        cls, error: Any
    ) -> tuple[Any, str | None, str | None]:
        err_sbx = getattr(error, "sandbox", None)
        err_id = getattr(err_sbx, "sandbox_id", None)
        if not isinstance(err_id, str) or not err_id:
            err_record = getattr(err_sbx, "record", None)
            raw_id = (
                err_record.get("sandbox_id") if isinstance(err_record, dict) else None
            )
            err_id = raw_id if isinstance(raw_id, str) and raw_id else None
        err_state = cls._handle_state(err_sbx) if err_sbx is not None else None
        return err_sbx, err_id, err_state

    @classmethod
    def _translate_sdk_error(cls, error: Any) -> RunfieldControllerError:
        code = str(getattr(error, "code", None) or "unknown_error")
        message = str(error) or code
        _, err_id, err_state = cls._extract_error_sandbox(error)
        return RunfieldControllerError(
            code,
            message,
            retryable=bool(getattr(error, "retryable", False)),
            status_code=getattr(error, "http_status", None),
            sandbox_id=err_id,
            sandbox_state=err_state,
        )

    def _resolve_proxy_settings(self) -> tuple[str | None, str]:
        raw_mode = (
            self._explicit_proxy_mode
            if self._explicit_proxy_mode is not None
            else os.environ.get("RUNFIELD_CLIENT_PROXY_MODE", "").strip()
        )
        raw_socket = (
            self._explicit_proxy_socket
            if self._explicit_proxy_socket is not None
            else os.environ.get("RUNFIELD_CLIENT_PROXY_SOCKET", "").strip()
        )
        resolved_socket = raw_socket or None
        if raw_mode == "direct" and self._explicit_proxy_socket is None:
            resolved_socket = None
        if raw_mode:
            resolved_mode = raw_mode
        elif resolved_socket is not None:
            resolved_mode = "uds"
        else:
            resolved_mode = "auto"
        return resolved_socket, resolved_mode

    def _config_key(self) -> tuple[Any, ...]:
        proxy_socket, proxy_mode = self._resolve_proxy_settings()
        proxy_bin = os.environ.get("RUNFIELD_CLIENT_PROXY_BIN", "").strip() or None
        return (
            self.endpoint,
            self.allow_insecure_http,
            proxy_socket,
            proxy_mode,
            proxy_bin,
            self.request_timeout_sec,
            self.transfer_timeout_sec,
        )

    def _shared_state(self) -> _LoopSharedState:
        _, _, sdk_client_cls, _ = _load_runfield_sdk()
        loop = asyncio.get_running_loop()
        per_loop = _LOOP_SHARED_STATES.get(loop)
        if per_loop is None:
            per_loop = {}
            _LOOP_SHARED_STATES[loop] = per_loop
        key = self._config_key()
        state = per_loop.get(key)
        if state is None:
            proxy_socket, proxy_mode = key[2], key[3]
            init_kwargs: dict[str, Any] = {
                "endpoint": self.endpoint,
                "allow_insecure_http": self.allow_insecure_http,
                "proxy_socket": proxy_socket,
                "proxy_mode": proxy_mode,
            }
            try:
                params = inspect.signature(sdk_client_cls).parameters
            except (TypeError, ValueError):
                params = {}
            if "timeout_seconds" in params or any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in params.values()
            ):
                init_kwargs["timeout_seconds"] = self.request_timeout_sec
            state = _LoopSharedState(sdk_client_cls(**init_kwargs))
            per_loop[key] = state
        self._sandboxes = state.sandboxes
        return state

    async def aclose(self) -> None:
        """Close the shared SDK client bound to the running event loop and config."""
        loop = asyncio.get_running_loop()
        per_loop = _LOOP_SHARED_STATES.get(loop)
        if not per_loop:
            return
        state = per_loop.pop(self._config_key(), None)
        if state is not None and hasattr(state.sdk_client, "aclose"):
            await state.sdk_client.aclose()

    @staticmethod
    def _handle_state(sbx: Any, record: dict[str, Any] | None = None) -> str | None:
        state = getattr(sbx, "state", None)
        if isinstance(state, str) and state:
            return state
        snap = record if isinstance(record, dict) else getattr(sbx, "record", None)
        if isinstance(snap, dict):
            raw = snap.get("state")
            if isinstance(raw, str):
                return raw
        return None

    async def _resolve_sandbox(
        self,
        sandbox_id: str,
        *,
        timeout_sec: float | None = None,
    ) -> tuple[_LoopSharedState, Any, bool]:
        sdk_error_cls = self._load_sdk_error()
        state = self._shared_state()
        existing = state.sandboxes.get(sandbox_id)
        if existing is not None:
            return state, existing, False
        if not hasattr(state.sdk_client, "get"):
            raise RunfieldControllerError(
                "not_found",
                f"sandbox handle is not registered: {sandbox_id}",
                retryable=False,
                status_code=404,
            )
        try:
            fetched = await state.sdk_client.get(
                sandbox_id,
                timeout_seconds=timeout_sec or self.request_timeout_sec,
            )
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error
        if self._handle_state(fetched) not in _INACTIVE_HANDLE_STATES:
            state.sandboxes[sandbox_id] = fetched
        return state, fetched, True

    async def create_sandbox(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Submit Sandbox creation (wait_until='accepted') and retain the SDK handle."""
        sdk_error_cls = self._load_sdk_error()
        state = self._shared_state()
        create_payload = {"backend": "container", **spec}
        request_id = create_payload.pop("request_id", None)
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("create_sandbox requires a non-empty string request_id")
        try:
            sbx = await state.sdk_client.create(
                create_payload,
                request_id=request_id,
                wait_until="accepted",
                timeout_seconds=self.request_timeout_sec,
            )
        except sdk_error_cls as error:
            err_sbx, err_id, err_state = self._extract_error_sandbox(error)
            if err_sbx is not None and err_id is not None:
                if err_state not in _INACTIVE_HANDLE_STATES:
                    state.sandboxes[err_id] = err_sbx
                else:
                    state.sandboxes.pop(err_id, None)
            raise self._translate_sdk_error(error) from error
        record = dict(sbx.record)
        sandbox_id = getattr(sbx, "sandbox_id", None) or record.get("sandbox_id")
        if isinstance(sandbox_id, str) and sandbox_id:
            state.sandboxes[sandbox_id] = sbx
        return record

    async def wait_sandbox_ready(
        self, sandbox_id: str, *, timeout_sec: float
    ) -> dict[str, Any]:
        """Wait for Sandbox readiness via UDS blocking wait or SDK direct polling."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            await sbx.wait_ready(timeout_seconds=timeout_sec)
        except sdk_error_cls as error:
            code = getattr(error, "code", None)
            if code == "timeout":
                raise TimeoutError(
                    f"Runfield Sandbox wait deadline exceeded: {sandbox_id}"
                ) from error
            if code == "state_conflict":
                terminal_state = self._handle_state(sbx)
                raise RuntimeError(
                    f"Runfield Sandbox entered terminal state {terminal_state}"
                ) from error
            raise self._translate_sdk_error(error) from error
        return dict(sbx.record)

    async def sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        """Refresh and return the latest Sandbox record snapshot."""
        sdk_error_cls = self._load_sdk_error()
        state, sbx, was_fetched = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        if not was_fetched:
            try:
                await sbx.refresh(timeout_seconds=self.request_timeout_sec)
            except sdk_error_cls as error:
                if getattr(error, "code", None) == "not_found":
                    state.sandboxes.pop(sandbox_id, None)
                raise self._translate_sdk_error(error) from error
        record = dict(sbx.record)
        if self._handle_state(sbx, record) in _INACTIVE_HANDLE_STATES:
            state.sandboxes.pop(sandbox_id, None)
        return record

    async def close_sandbox(
        self,
        sandbox_id: str,
        *,
        wait: bool = True,
        timeout_sec: float = 120,
    ) -> dict[str, Any]:
        """Close the Sandbox and evict its handle once terminal close is reached."""
        sdk_error_cls = self._load_sdk_error()
        state, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=min(timeout_sec, self.request_timeout_sec)
        )
        try:
            await sbx.close(wait=wait, timeout_seconds=timeout_sec)
        except sdk_error_cls as error:
            code = getattr(error, "code", None)
            if code == "not_found":
                state.sandboxes.pop(sandbox_id, None)
            if code == "timeout":
                raise TimeoutError(
                    f"Runfield Sandbox close deadline exceeded: {sandbox_id}"
                ) from error
            raise self._translate_sdk_error(error) from error
        record = dict(sbx.record)
        if self._handle_state(sbx, record) in _INACTIVE_HANDLE_STATES:
            state.sandboxes.pop(sandbox_id, None)
        return record

    async def create_execution(
        self, sandbox_id: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        """Submit a non-TTY asynchronous execution (202 Accepted)."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            record = await sbx.exec(
                spec["argv"],
                request_id=spec.get("request_id"),
                env=spec.get("env"),
                working_directory=spec.get("working_directory"),
                output_limit_bytes=spec.get("output_limit_bytes", MAX_OUTPUT_READ_BYTES),
                priority=spec.get("priority"),
                timeout_seconds=self.request_timeout_sec,
            )
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error
        return dict(record)

    async def wait_execution(
        self,
        sandbox_id: str,
        execution_id: str,
        *,
        timeout_sec: float,
        output_limit_bytes: int,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Wait for execution completion and collect bounded stdout and stderr."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            res = await sbx.wait_execution(
                execution_id,
                output_limit_bytes=output_limit_bytes,
                include_output=True,
                cancel_on_disconnect=False,
                timeout_seconds=timeout_sec,
            )
        except sdk_error_cls as error:
            code = getattr(error, "code", None)
            if code == "timeout":
                raise TimeoutError(
                    f"Runfield execution wait deadline exceeded: {execution_id}"
                ) from error
            if code == "resource_limit_exceeded":
                raise RuntimeError(
                    "Runfield execution output was truncated or exceeded limit"
                ) from error
            raise self._translate_sdk_error(error) from error
        return dict(res.execution), res.stdout, res.stderr

    async def execution_status(
        self, sandbox_id: str, execution_id: str
    ) -> dict[str, Any]:
        """Query the latest execution record snapshot."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=self.request_timeout_sec
        )
        try:
            record = await sbx.get_execution(
                execution_id, timeout_seconds=self.request_timeout_sec
            )
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error
        return dict(record)

    async def cancel_execution(
        self, sandbox_id: str, execution_id: str
    ) -> dict[str, Any]:
        """Cancel a queued or running (aether/v1) execution."""
        sdk_error_cls = self._load_sdk_error()
        _, sbx, _ = await self._resolve_sandbox(
            sandbox_id, timeout_sec=min(self.request_timeout_sec, 15)
        )
        try:
            record = await sbx.cancel_execution(
                execution_id, timeout_seconds=min(self.request_timeout_sec, 15)
            )
        except sdk_error_cls as error:
            raise self._translate_sdk_error(error) from error
        return dict(record)


__all__ = [
    "API_VERSION",
    "MAX_ARCHIVE_BYTES",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_OUTPUT_READ_BYTES",
    "RunfieldControllerClient",
    "RunfieldControllerError",
]
