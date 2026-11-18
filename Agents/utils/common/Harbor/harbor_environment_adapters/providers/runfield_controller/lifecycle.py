"""Map Harbor lifecycle calls to Runfield create/wait_ready/close operations."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

_CREATE_FAILURE_STATES = {"create_failed", "recovery_required", "closed", "exited"}
_CLOSE_TERMINAL_STATES = {"closed", "create_failed"}


class RunfieldLifecycleMixin:
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

    async def start(self, force_build: bool) -> None:
        if force_build:
            self.logger.warning(
                "Runfield uses the immutable image prepared by Agent Fleet; "
                "Harbor force_build is not forwarded to Controller"
            )
        self._validate_definition()
        spec = self._create_spec()
        try:
            record = await self._client.create_sandbox(spec)
            sandbox_id = record.get("sandbox_id")
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise RuntimeError("Runfield Controller returned no sandbox identity")
            self._sandbox_id = sandbox_id
            if record.get("state") in _CREATE_FAILURE_STATES:
                raise RuntimeError(
                    "Runfield Sandbox entered terminal state "
                    f"{record.get('state')}"
                )
            observation = record.get("observation") or {}
            if (
                record.get("state") != "ready"
                or observation.get("reachable") is not True
            ):
                record = await self._client.wait_sandbox_ready(
                    sandbox_id, timeout_sec=self._ready_timeout_sec
                )
            if record.get("state") in _CREATE_FAILURE_STATES:
                raise RuntimeError(
                    "Runfield Sandbox entered terminal state "
                    f"{record.get('state')}"
                )
            observation = record.get("observation") or {}
            if (
                record.get("state") != "ready"
                or observation.get("reachable") is not True
            ):
                raise RuntimeError(
                    "Runfield Sandbox is ready but owning Node is unreachable"
                )
            self._harbor_dirs_prepared = False
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
                "Runfield backend does not retain stopped Sandboxes; closing %s",
                sandbox_id,
            )
        record = await self._client.close_sandbox(
            sandbox_id,
            wait=True,
            timeout_sec=self._close_timeout_sec,
        )
        if isinstance(record, dict) and record.get("state") not in _CLOSE_TERMINAL_STATES:
            raise RuntimeError(
                f"Runfield Sandbox did not reach closed state: {record.get('state')}"
            )
        if self._sandbox_id == sandbox_id:
            self._sandbox_id = ""
