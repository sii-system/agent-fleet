"""Map Harbor commands to asynchronous, non-TTY DSec execution resources."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from .client import MAX_OUTPUT_READ_BYTES, DsecControllerError

_EXECUTION_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "canceled",
    "recovery_required",
}
_MAX_DIRECT_COMMAND_BYTES = 4096
_BACKGROUND_ABORT_TASKS: set[asyncio.Task[None]] = set()


class DsecExecutionMixin:
    """Execute commands while preserving streams, truncation, and identity semantics."""

    async def _close_after_execution_abort(self, reason: str) -> None:
        """Close the Sandbox only as fallback when single-execution cancel cannot guarantee cleanup.

        Used only when ``close_on_timeout=True`` and single-execution
        cancellation fails (for example on ``docker-exec/v1``) or no
        ``execution_id`` was returned before cancellation. Best-effort probes
        pass ``close_on_timeout=False`` so a timeout never destroys the shared
        verifier Sandbox.
        """
        try:
            await asyncio.shield(self.stop(delete=True))
        except Exception:
            self.logger.exception(
                "failed to close DSec Sandbox after execution %s", reason
            )

    async def _run_execution_abort_sequence(
        self,
        execution_id: str | None,
        record: dict[str, Any] | None,
        *,
        close_on_timeout: bool,
        reason: str,
    ) -> None:
        """Cancel a single execution on wait timeout/cancellation, falling back cleanly.

        Skips cancel when ``record`` already reached a clean terminal state
        (e.g., ``CancelledError`` during stream reading). Otherwise attempts
        ``cancel_execution`` when ``execution_id`` is known (covering queued and
        ``aether/v1`` running executions). On ``state_conflict``, inspects
        ``execution_status`` to distinguish natural completion from an active
        non-cancelable execution (e.g. ``docker-exec/v1``) before falling back
        to closing the Sandbox when ``close_on_timeout=True``.
        """
        clean_terminal_states = {"succeeded", "failed", "canceled"}
        state = record.get("state") if isinstance(record, dict) else None
        if state in clean_terminal_states:
            return
        if state == "recovery_required":
            if close_on_timeout:
                await self._close_after_execution_abort(reason)
            return
        if self._sandbox_id and execution_id:
            try:
                canceled = await self._client.cancel_execution(
                    self._sandbox_id, execution_id
                )
                if isinstance(canceled, dict) and canceled.get("state") == "canceled":
                    return
            except DsecControllerError as exc:
                if exc.code == "state_conflict" and close_on_timeout:
                    try:
                        latest = await self._client.execution_status(
                            self._sandbox_id, execution_id
                        )
                        if (
                            isinstance(latest, dict)
                            and latest.get("state") in clean_terminal_states
                        ):
                            return
                    except Exception:
                        self.logger.debug(
                            "failed to probe execution status after cancel state_conflict for %s/%s",
                            self._sandbox_id,
                            execution_id,
                            exc_info=True,
                        )
                if close_on_timeout:
                    self.logger.warning(
                        "single-execution cancel failed for %s/%s (%s, retryable=%s)",
                        self._sandbox_id,
                        execution_id,
                        exc.code,
                        exc.retryable,
                    )
                else:
                    self.logger.debug(
                        "best-effort cancel_execution skipped for %s/%s (%s, retryable=%s)",
                        self._sandbox_id,
                        execution_id,
                        exc.code,
                        exc.retryable,
                    )
            except Exception:
                if close_on_timeout:
                    self.logger.exception(
                        "unexpected error cancelling DSec execution %s/%s",
                        self._sandbox_id,
                        execution_id,
                    )
                else:
                    self.logger.debug(
                        "best-effort cancel_execution skipped for %s/%s",
                        self._sandbox_id,
                        execution_id,
                        exc_info=True,
                    )
        if close_on_timeout:
            await self._close_after_execution_abort(reason)

    async def _abort_execution_on_wait_exit(
        self,
        execution_id: str | None,
        record: dict[str, Any] | None,
        *,
        close_on_timeout: bool,
        reason: str,
    ) -> None:
        """Run the full abort sequence in a shielded task so repeated cancellation cannot skip fallback."""
        task = asyncio.create_task(
            self._run_execution_abort_sequence(
                execution_id,
                record,
                close_on_timeout=close_on_timeout,
                reason=reason,
            )
        )
        _BACKGROUND_ABORT_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_ABORT_TASKS.discard)
        await asyncio.shield(task)

    def _execution_request_id(self, command: str) -> str:
        self._execution_sequence += 1
        digest = hashlib.sha256(
            f"{self._sandbox_id}\0{self._execution_sequence}\0{command}".encode()
        ).hexdigest()
        return f"harbor-exec-{digest[:40]}"

    def _resolve_stat_probe_path(self, path: str) -> str | None:
        """Return a clean path for external stat, or None when kernel resolution is required."""
        if path.startswith("/"):
            candidate = path
        else:
            workdir = getattr(getattr(self, "task_env_config", None), "workdir", None) or "/"
            if not workdir.startswith("/"):
                return None
            candidate = workdir if path == "." else f"{workdir.rstrip('/')}/{path}"
        segments = [segment for segment in candidate.split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            return None
        if not segments:
            return "/"
        return "/" + "/".join(segments)

    async def _legacy_probe_path_kind(
        self, path: str, *, require_dir: bool, user: str | int | None = None
    ) -> bool:
        """Probe container path type inside guest execution without closing on timeout."""
        flag = "-d" if require_dir else "-f"
        result = await self.exec(
            f"test {flag} {shlex.quote(path)}",
            user=user,
            close_on_timeout=False,
        )
        return result.return_code == 0

    async def _probe_path_kind(
        self, path: str, *, require_dir: bool, user: str | int | None = None
    ) -> bool:
        """Probe container path type using 0-exec stat with kernel-exec fallback.

        Clean paths without ``.`` or ``..`` traversal segments query
        Controller ``stat_path`` with symlink-following semantics. Paths needing
        in-container kernel resolution (``.``/``..`` segments or relative symlinks
        without server ``target_type``) and older Controllers without
        ``/archive/stat`` fall back to ``exec(..., close_on_timeout=False)``.
        """
        if not self._sandbox_id:
            raise RuntimeError("DSec Sandbox is not running")
        effective_user = self._resolve_user(user)
        if effective_user not in {None, "root", "0", 0}:
            raise ValueError(
                "DSec backend only supports the root execution identity"
            )
        if (
            not isinstance(path, str)
            or not path
            or any(ch in path for ch in ("\x00", "\r", "\n"))
        ):
            raise ValueError("DSec probe path must be a non-empty valid path")
        if not require_dir and (
            path in {".", "/"} or path.endswith(("/", "/."))
        ):
            return False
        if getattr(self, "_archive_stat_supported", True) is False:
            return await self._legacy_probe_path_kind(
                path, require_dir=require_dir, user=user
            )
        target = self._resolve_stat_probe_path(path)
        if target is None:
            return await self._legacy_probe_path_kind(
                path, require_dir=require_dir, user=user
            )
        try:
            stat = await self._client.stat_path(self._sandbox_id, target)
        except DsecControllerError as exc:
            if (
                exc.code == "unsupported_capability"
                or exc.status_code in {404, 405, 501}
                or exc.code in {"not_found", "404", "405", "501"}
            ):
                result = await self._legacy_probe_path_kind(
                    path, require_dir=require_dir, user=user
                )
                self._archive_stat_supported = False
                return result
            raise
        if not stat.get("exists"):
            return False
        expected = "directory" if require_dir else "file"
        entry_type = stat.get("type")
        if entry_type == expected:
            return True
        if entry_type == "symlink":
            target_type = stat.get("target_type")
            if target_type:
                return target_type == expected
            if not stat.get("link_target"):
                return await self._legacy_probe_path_kind(
                    path, require_dir=require_dir, user=user
                )
            return False
        return False

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        """Probe whether ``path`` is a directory matching POSIX ``test -d``.

        Clean paths use 0-exec container-external stat without consuming
        execution queue/spool budget; older Controllers without ``/archive/stat``
        or paths needing kernel resolution (such as ``..`` segments or relative
        symlinks) fall back to ``exec(..., close_on_timeout=False)``. Missing
        paths return False while Sandbox lifecycle errors propagate.
        """
        return await self._probe_path_kind(path, require_dir=True, user=user)

    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        """Probe whether ``path`` is a regular file matching POSIX ``test -f``.

        See ``is_dir``: clean paths use 0-exec container-external stat (and
        directory-only forms such as trailing ``/`` return False immediately),
        while older Controllers or special paths fall back to
        ``exec(..., close_on_timeout=False)``.
        """
        return await self._probe_path_kind(path, require_dir=False, user=user)

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
        priority: str | None = None,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Execute one argv and preserve the caller's timeout ownership.

        ``close_on_timeout`` controls whether exceeding the wait deadline (or
        cancellation of the wait) closes the Sandbox. Keep it True only for
        critical agent/verifier commands that cannot leave an orphan execution.
        Best-effort callers pass False; when ``execution_id`` is known they
        attempt best-effort queued cancellation and handle TimeoutError locally.
        """
        if not self._sandbox_id:
            raise RuntimeError("DSec Sandbox is not running")
        request: dict[str, Any] = {
            "request_id": self._execution_request_id("\0".join(argv)),
            "argv": argv,
            "output_limit_bytes": output_limit_bytes or self._exec_output_limit_bytes,
        }
        if cwd:
            request["working_directory"] = cwd
        if env:
            request["env"] = env
        if priority is not None:
            request["priority"] = priority
        execution_id: str | None = None
        record: dict[str, Any] | None = None
        try:
            record = await self._client.create_execution(self._sandbox_id, request)
            raw_execution_id = record.get("execution_id")
            if not isinstance(raw_execution_id, str) or not raw_execution_id:
                raise RuntimeError("DSec Controller returned no execution identity")
            execution_id = raw_execution_id
            deadline = time.monotonic() + (
                timeout_sec or self._default_exec_timeout_sec
            )
            while record.get("state") not in _EXECUTION_TERMINAL_STATES:
                if time.monotonic() >= deadline:
                    await self._abort_execution_on_wait_exit(
                        execution_id,
                        record,
                        close_on_timeout=close_on_timeout,
                        reason="timeout",
                    )
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
            await self._abort_execution_on_wait_exit(
                execution_id,
                record,
                close_on_timeout=close_on_timeout,
                reason="cancellation",
            )
            raise

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
        close_on_timeout: bool = True,
        priority: str | None = None,
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
                priority=priority,
            )
        else:
            record, stdout, stderr = await self._execute_materialized_command(
                command_bytes,
                cwd=effective_cwd,
                env=effective_env,
                timeout_sec=timeout_sec,
                close_on_timeout=close_on_timeout,
                priority=priority,
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
        priority: str | None = None,
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
            priority=priority,
        )
