"""Map Harbor start/stop to the durable DSec create/status/close state machine."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import requests

from .client import DsecControllerError

_CREATE_FAILURE_STATES = {"create_failed", "recovery_required", "closed", "exited"}
_CLOSE_TERMINAL_STATES = {"closed", "create_failed"}


class DsecLifecycleMixin:
    """Manage one Sandbox per trial through the public lifecycle state machine."""

    def _sandbox_request_id(self) -> str:
        context = str(getattr(self, "context_id", "") or "")
        material = f"{context}\0{self.session_id}\0{self._image_ref}"
        return "harbor-sandbox-" + hashlib.sha256(material.encode()).hexdigest()[:40]

    def _create_spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "request_id": self._sandbox_request_id(),
            "image": self._image_ref,
            "process": {
                "argv": [
                    "/bin/sh",
                    "-lc",
                    "trap : TERM INT; sleep infinity & wait",
                ],
                # Harbor 0.18 owns task-level environment in _persistent_env;
                # per-exec overlays are intentionally not active at creation.
                "env": dict(self._persistent_env),
                "user": "root",
                "working_directory": "/",
            },
            "resources": self._resource_request(),
            "lifecycle": {"ttl_seconds": self._ttl_seconds},
            "image_delivery": {
                "mode": "lazy-required",
                "fallback": "none",
            },
        }
        if self._network_mode != "none":
            spec["network_mode"] = self._network_mode
        return spec

    async def _create_with_stable_identity(self) -> dict[str, Any]:
        spec = self._create_spec()
        last_error: Exception | None = None
        for attempt in range(self._request_attempts):
            try:
                return await self._client.create_sandbox(spec)
            except DsecControllerError as error:
                last_error = error
                if not error.retryable or attempt + 1 == self._request_attempts:
                    raise
            except requests.RequestException as error:
                last_error = error
                if attempt + 1 == self._request_attempts:
                    raise
            await asyncio.sleep(min(2**attempt, 5))
        raise RuntimeError("DSec create retry loop exhausted") from last_error

    async def _wait_sandbox_state(
        self,
        wanted: set[str],
        *,
        timeout_sec: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        while True:
            record = await self._client.sandbox_status(self._sandbox_id)
            state = record.get("state")
            if state in wanted:
                return record
            if state in _CREATE_FAILURE_STATES:
                raise RuntimeError(f"DSec Sandbox entered terminal state {state}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"DSec Sandbox wait deadline exceeded: {self._sandbox_id}"
                )
            await asyncio.sleep(self._poll_interval_sec)

    async def start(self, force_build: bool) -> None:
        if force_build:
            self.logger.warning(
                "DSec uses the immutable image prepared by Agent Fleet; "
                "Harbor force_build is not forwarded to Controller"
            )
        self._validate_definition()
        try:
            record = await self._create_with_stable_identity()
            sandbox_id = record.get("sandbox_id")
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise RuntimeError("DSec Controller returned no sandbox identity")
            self._sandbox_id = sandbox_id
            if record.get("state") in _CREATE_FAILURE_STATES:
                raise RuntimeError(
                    "DSec Sandbox entered terminal state "
                    f"{record.get('state')}"
                )
            if record.get("state") != "ready":
                record = await self._wait_sandbox_state(
                    {"ready"}, timeout_sec=self._ready_timeout_sec
                )
            observation = record.get("observation") or {}
            if observation.get("reachable") is not True:
                raise RuntimeError(
                    "DSec Sandbox is ready but owning Node is unreachable"
                )
            setup = await self.exec(
                "mkdir -p /logs/agent /logs/verifier /logs/artifacts /artifacts "
                "&& chmod 777 /logs/agent /logs/verifier /logs/artifacts /artifacts",
                cwd="/",
                timeout_sec=60,
                user="root",
            )
            if setup.return_code != 0:
                raise RuntimeError("failed to initialize Harbor directories")
            await self._upload_environment_dir_after_start()
            await self._materialize_read_only_mounts()
        except BaseException:
            if self._sandbox_id:
                await asyncio.shield(self.stop(delete=True))
            raise

    async def stop(self, delete: bool) -> None:
        sandbox_id = self._sandbox_id
        if not sandbox_id:
            return
        if not delete:
            self.logger.warning(
                "DSec backend does not retain stopped Sandboxes; closing %s",
                sandbox_id,
            )
        record = await self._client.close_sandbox(sandbox_id)
        if record.get("state") not in _CLOSE_TERMINAL_STATES:
            await self._wait_sandbox_state(
                _CLOSE_TERMINAL_STATES,
                timeout_sec=self._close_timeout_sec,
            )
        if self._sandbox_id == sandbox_id:
            self._sandbox_id = ""
