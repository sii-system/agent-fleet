"""Map Harbor commands to asynchronous, non-TTY DSec execution resources."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from .client import MAX_OUTPUT_READ_BYTES


_EXECUTION_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "canceled",
    "recovery_required",
}
_MAX_DIRECT_COMMAND_BYTES = 4096

# TODO(dsec-exec-cancel): DSec executions have no cancel API today. The adapter
# therefore closes the whole Sandbox when a critical execution times out, which
# is the safe fallback for agent/verifier commands. Best-effort artifact probes
# must not take that lifecycle ownership; see the is_dir/is_file overrides and
# the close_on_timeout contract below. Replace this fallback with a first-class
# execution-cancel operation (or a non-exec metadata probe) when available.


class DsecExecutionMixin:
    """Execute commands while preserving streams, truncation, and identity semantics."""

    async def _close_after_execution_abort(self, reason: str) -> None:
        """Close the Sandbox only when the caller owns the execution lifecycle.

        This is the last-resort fallback for critical agent/verifier commands,
        because DSec executions cannot be cancelled safely today. It must not be
        used by best-effort probes: closing the Sandbox destroys the shared
        verifier environment and makes later archive uploads target a missing
        Sandbox. TODO(dsec-exec-cancel): cancel the execution instead of closing
        the Sandbox once the protocol exposes cancellation.
        """
        try:
            await asyncio.shield(self.stop(delete=True))
        except Exception:
            self.logger.exception(
                "failed to close DSec Sandbox after execution %s", reason
            )

    def _execution_request_id(self, command: str) -> str:
        self._execution_sequence += 1
        digest = hashlib.sha256(
            f"{self._sandbox_id}\0{self._execution_sequence}\0{command}".encode()
        ).hexdigest()
        return f"harbor-exec-{digest[:40]}"

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        """Probe an artifact path without taking Sandbox lifecycle ownership.

        Harbor's base implementation uses a 10-second exec timeout. Under DSec
        execution-queue pressure that timeout is expected, and closing the
        Sandbox here would clear ``_sandbox_id`` before the shared verifier runs.
        The 300-second bound is a temporary probe budget; a timeout remains a
        local best-effort failure and must not tear down the environment.
        TODO(dsec-artifact-probe): use Controller archive/mount metadata or a
        first-class non-exec probe instead of a container exec.
        """
        result = await self.exec(
            self._path_kind_check_command(path, require_dir=True),
            timeout_sec=300,
            user=user,
            close_on_timeout=False,
        )
        return result.return_code == 0

    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        """Probe an artifact path without taking Sandbox lifecycle ownership.

        See ``is_dir``: this is a best-effort artifact probe, not a lifecycle
        operation. A timeout must leave the shared verifier Sandbox alive.
        """
        result = await self.exec(
            self._path_kind_check_command(path, require_dir=False),
            timeout_sec=300,
            user=user,
            close_on_timeout=False,
        )
        return result.return_code == 0

    async def _read_execution_stream(
        self, record: dict[str, Any], stream: str
    ) -> bytes:
        metadata = record.get(stream)
        if not isinstance(metadata, dict):
            raise TypeError(f"DSec execution is missing {stream} metadata")
        if metadata.get("truncated") is True:
            raise RuntimeError(f"DSec execution {stream} was truncated")
        committed = metadata.get("committed_bytes")
        if not isinstance(committed, int) or committed < 0:
            raise RuntimeError(f"DSec execution returned invalid {stream} length")
        chunks: list[bytes] = []
        for offset in range(0, committed, MAX_OUTPUT_READ_BYTES):
            length = min(MAX_OUTPUT_READ_BYTES, committed - offset)
            chunk = await self._client.execution_output(
                self._sandbox_id,
                record["execution_id"],
                stream,
                offset,
                length,
            )
            if len(chunk) != length:
                raise RuntimeError("DSec execution output range is incomplete")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _execute_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        output_limit_bytes: int | None = None,
        close_on_timeout: bool = True,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Execute one argv and preserve the caller's timeout ownership.

        ``close_on_timeout`` controls whether exceeding the wait deadline (or
        cancellation of the wait) closes the Sandbox. Keep it True only for
        critical agent/verifier commands that cannot leave an orphan execution.
        Best-effort callers pass False and handle the TimeoutError locally.
        """
        if not self._sandbox_id:
            raise RuntimeError("DSec Sandbox is not running")
        request = {
            "request_id": self._execution_request_id("\0".join(argv)),
            "argv": argv,
            "output_limit_bytes": output_limit_bytes or self._exec_output_limit_bytes,
        }
        if cwd:
            request["working_directory"] = cwd
        if env:
            request["env"] = env
        try:
            record = await self._client.create_execution(self._sandbox_id, request)
            execution_id = record.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                raise RuntimeError("DSec Controller returned no execution identity")
            deadline = time.monotonic() + (
                timeout_sec or self._default_exec_timeout_sec
            )
            while record.get("state") not in _EXECUTION_TERMINAL_STATES:
                if time.monotonic() >= deadline:
                    if close_on_timeout:
                        await self._close_after_execution_abort("timeout")
                    raise TimeoutError(
                        f"DSec execution wait deadline exceeded: {execution_id}"
                    )
                await asyncio.sleep(self._poll_interval_sec)
                record = await self._client.execution_status(
                    self._sandbox_id, execution_id
                )
            stdout, stderr = await asyncio.gather(
                self._read_execution_stream(record, "stdout"),
                self._read_execution_stream(record, "stderr"),
            )
            return record, stdout, stderr
        except asyncio.CancelledError:
            if close_on_timeout:
                await self._close_after_execution_abort("cancellation")
            raise

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
        close_on_timeout: bool = True,
    ) -> ExecResult:
        """Run a Harbor shell command under the root-only DSec backend.

        ``close_on_timeout`` is forwarded to ``_execute_argv``. Only critical
        lifecycle callers should use the default True; best-effort probes pass
        False so a timeout does not destroy the shared Sandbox.
        """
        effective_user = self._resolve_user(user)
        if effective_user not in {None, "root", "0", 0}:
            raise ValueError(
                "DSec backend only supports the root execution identity"
            )
        effective_cwd = cwd or self.task_env_config.workdir or "/"
        effective_env = self._merge_env(env)
        command_bytes = command.encode("utf-8")
        if len(command_bytes) <= _MAX_DIRECT_COMMAND_BYTES:
            argv = ["/bin/bash", "-lc", command]
            record, stdout, stderr = await self._execute_argv(
                argv,
                cwd=effective_cwd,
                env=effective_env,
                timeout_sec=timeout_sec,
                close_on_timeout=close_on_timeout,
            )
        else:
            record, stdout, stderr = await self._execute_materialized_command(
                command_bytes,
                cwd=effective_cwd,
                env=effective_env,
                timeout_sec=timeout_sec,
                close_on_timeout=close_on_timeout,
            )
        state = record.get("state")
        if state == "recovery_required":
            raise RuntimeError("DSec execution requires recovery")
        code = record.get("exit_code")
        if not isinstance(code, int):
            code = 1
        result = ExecResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            return_code=code,
        )
        callback = self._output_callback()
        if callback is not None:
            if result.stdout:
                await callback(result.stdout, "stdout")
            if result.stderr:
                await callback(result.stderr, "stderr")
        return result

    async def _execute_materialized_command(
        self,
        command: bytes,
        *,
        cwd: str,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        close_on_timeout: bool = True,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Upload an oversized Bash program and execute it with bounded argv."""
        remote_path = f"/tmp/.harbor-command-{uuid.uuid4().hex}.sh"
        with tempfile.TemporaryDirectory(prefix="harbor-dsec-command-") as temporary:
            local_path = Path(temporary) / "command.sh"
            local_path.write_bytes(command)
            local_path.chmod(0o600)
            await self.upload_file(local_path, remote_path)
        # The script is owned by this Sandbox and reclaimed on close. Removing it
        # must not introduce a guest Python/shell dependency for host-side cleanup.
        return await self._execute_argv(
            ["/bin/bash", remote_path],
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            close_on_timeout=close_on_timeout,
        )
