"""Map Harbor commands to Runfield asynchronous non-TTY executions."""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
import shlex
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from .client import RunfieldControllerError

_EXECUTION_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "canceled",
    "recovery_required",
}
_MAX_DIRECT_COMMAND_BYTES = 4096
_MAX_INLINE_CHUNKED_COMMAND_BYTES = 98304
_MAX_EXECUTION_ARGV_ITEMS = 256
_HARBOR_DIRS_BOOTSTRAP = (
    "command -p mkdir -p -m 0777 "
    "/logs/agent /logs/verifier /logs/artifacts /artifacts || exit $?; "
)
_DIRECT_HARBOR_DIRS_BOOTSTRAP = (
    f'{_HARBOR_DIRS_BOOTSTRAP}exec /bin/bash -c "$1"'
)
_MATERIALIZED_HARBOR_DIRS_BOOTSTRAP = (
    f'{_HARBOR_DIRS_BOOTSTRAP}exec /bin/bash "$1"'
)
_DEFERRED_HARBOR_DIRS = frozenset(
    {
        "/logs",
        "/logs/agent",
        "/logs/verifier",
        "/logs/artifacts",
        "/artifacts",
    }
)
_BACKGROUND_ABORT_TASKS: set[asyncio.Task[None]] = set()


def _pick_scratch_shell_var(*envs: Mapping[str, str] | None) -> str:
    """Return an unexported scratch Bash identifier absent from caller environments."""
    blocked: set[str] = set()
    for env in envs:
        if env:
            blocked.update(env.keys())
    while True:
        candidate = f"__rf_cmd_{uuid.uuid4().hex[:16]}"
        if candidate not in blocked:
            return candidate


def _build_chunked_bash_bootstrap(
    scratch_var: str, *, include_dir_bootstrap: bool = False
) -> str:
    """Build an environment-preserving positional-chunk Bash bootstrap."""
    prefix = _HARBOR_DIRS_BOOTSTRAP if include_dir_bootstrap else ""
    return (
        f'{prefix}{scratch_var}=""; '
        f'while [ "$#" -gt 0 ]; do {scratch_var}+="$1"; shift; done; '
        f'export -n {scratch_var}; exec /bin/bash -c "${scratch_var}"'
    )


def _is_deferred_harbor_tree(path: str) -> bool:
    """Return True when an absolute POSIX path enters /logs or /artifacts."""
    trimmed = "/" + path.lstrip("/")
    if trimmed in {"/logs", "/artifacts"} or trimmed.startswith(
        ("/logs/", "/artifacts/")
    ):
        return True
    norm = posixpath.normpath(trimmed)
    return norm in {"/logs", "/artifacts"} or norm.startswith(
        ("/logs/", "/artifacts/")
    )


def _cwd_targets_deferred_harbor_dir(cwd: str, workdir: str | None) -> bool:
    """Return True when exec cwd resolves within deferred /logs or /artifacts."""
    if cwd.startswith("/"):
        return _is_deferred_harbor_tree(cwd)
    base = workdir or "/"
    base_abs = base if base.startswith("/") else f"/{base}"
    if _is_deferred_harbor_tree(base_abs):
        return True
    return _is_deferred_harbor_tree(posixpath.join(base_abs, cwd))


def _split_utf8_chunks(
    command_bytes: bytes, max_chunk_bytes: int = _MAX_DIRECT_COMMAND_BYTES
) -> list[str]:
    """Split UTF-8 bytes on valid codepoint boundaries within max_chunk_bytes."""
    chunks: list[str] = []
    offset = 0
    total = len(command_bytes)
    while offset < total:
        end = min(offset + max_chunk_bytes, total)
        if end < total:
            while end > offset and (command_bytes[end] & 0xC0) == 0x80:
                end -= 1
            if end == offset:
                raise ValueError("UTF-8 sequence exceeds chunk byte limit")
        chunks.append(command_bytes[offset:end].decode("utf-8"))
        offset = end
    return chunks


def _can_skip_execution_wait(record: dict[str, Any]) -> bool:
    """Return True when an accepted record is already terminal with empty streams."""
    if record.get("state") not in _EXECUTION_TERMINAL_STATES:
        return False
    for stream in ("stdout", "stderr"):
        metadata = record.get(stream)
        if not isinstance(metadata, dict):
            return False
        if metadata.get("truncated") is not False:
            return False
        if metadata.get("committed_bytes") != 0:
            return False
    return True


class RunfieldExecutionMixin:
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
                "failed to close Runfield Sandbox after execution %s", reason
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
            except RunfieldControllerError as exc:
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
                        "unexpected error cancelling Runfield execution %s/%s",
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
            raise RuntimeError("Runfield Sandbox is not running")
        effective_user = self._resolve_user(user)
        if effective_user not in {None, "root", "0", 0}:
            raise ValueError(
                "Runfield backend only supports the root execution identity"
            )
        if (
            not isinstance(path, str)
            or not path
            or any(ch in path for ch in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Runfield probe path must be a non-empty valid path")
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
        except RunfieldControllerError as exc:
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
            if (
                getattr(self, "_harbor_dirs_prepared", True) is False
                and target in _DEFERRED_HARBOR_DIRS
            ):
                return require_dir
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

    async def _execute_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        output_limit_bytes: int | None = None,
        close_on_timeout: bool = True,
        priority: str | None = None,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Execute one argv via two-phase submit + wait while preserving timeout ownership.

        ``close_on_timeout`` controls whether exceeding the wait deadline (or
        cancellation of the wait) closes the Sandbox as fallback when
        single-command cancellation cannot guarantee cleanup. Best-effort
        callers pass False so a timeout never destroys the shared Sandbox.
        """
        if not self._sandbox_id:
            raise RuntimeError("Runfield Sandbox is not running")
        effective_limit = output_limit_bytes or self._exec_output_limit_bytes
        effective_timeout = timeout_sec or self._default_exec_timeout_sec
        request: dict[str, Any] = {
            "request_id": self._execution_request_id("\0".join(argv)),
            "argv": argv,
            "output_limit_bytes": effective_limit,
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
                raise RuntimeError("Runfield Controller returned no execution identity")
            execution_id = raw_execution_id
            if _can_skip_execution_wait(record):
                return record, b"", b""
            record, stdout, stderr = await self._client.wait_execution(
                self._sandbox_id,
                execution_id,
                timeout_sec=effective_timeout,
                output_limit_bytes=effective_limit,
            )
            for stream in ("stdout", "stderr"):
                if (record.get(stream) or {}).get("truncated") is True:
                    raise RuntimeError(f"Runfield execution {stream} was truncated")
            return record, stdout, stderr
        except TimeoutError:
            await self._abort_execution_on_wait_exit(
                execution_id,
                record,
                close_on_timeout=close_on_timeout,
                reason="timeout",
            )
            raise
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
        timeout_sec: float | None = None,
        user: str | int | None = None,
        close_on_timeout: bool = True,
        priority: str | None = None,
    ) -> ExecResult:
        """Run a Harbor shell command under the root-only Runfield backend.

        Commands up to 4 KiB run directly via ``/bin/bash -c``; commands in
        ``(4 KiB, 96 KiB]`` without NUL bytes split across UTF-8 codepoint-safe
        ``<= 4 KiB`` positional arguments (0 archive RPCs); larger commands or
        scripts containing NUL fall back to ``_execute_materialized_command``.
        """
        effective_user = self._resolve_user(user)
        if effective_user not in {None, "root", "0", 0}:
            raise ValueError(
                "Runfield backend only supports the root execution identity"
            )
        workdir = getattr(getattr(self, "task_env_config", None), "workdir", None)
        effective_cwd = cwd or workdir or "/"
        effective_env = self._merge_env(env)
        needs_dir_bootstrap = (
            getattr(self, "_harbor_dirs_prepared", True) is False
        )
        if needs_dir_bootstrap and _cwd_targets_deferred_harbor_dir(
            effective_cwd, workdir
        ):
            await self._prepare_harbor_directories()
            self._harbor_dirs_prepared = True
            needs_dir_bootstrap = False
        command_bytes = command.encode("utf-8")
        if (
            len(command_bytes) <= _MAX_DIRECT_COMMAND_BYTES
            and "\x00" not in command
        ):
            # Harbor supplies the execution environment explicitly. A login
            # shell would source task-image profiles that are outside this
            # contract and can alter or even break otherwise valid commands.
            argv = (
                [
                    "/bin/bash",
                    "-c",
                    _DIRECT_HARBOR_DIRS_BOOTSTRAP,
                    "bash",
                    command,
                ]
                if needs_dir_bootstrap
                else ["/bin/bash", "-c", command]
            )
            record, stdout, stderr = await self._execute_argv(
                argv,
                cwd=effective_cwd,
                env=effective_env,
                timeout_sec=timeout_sec,
                close_on_timeout=close_on_timeout,
                priority=priority,
            )
        elif (
            len(command_bytes) <= _MAX_INLINE_CHUNKED_COMMAND_BYTES
            and "\x00" not in command
            and len(chunks := _split_utf8_chunks(command_bytes)) + 4
            <= _MAX_EXECUTION_ARGV_ITEMS
        ):
            scratch_var = _pick_scratch_shell_var(
                effective_env, getattr(self, "_persistent_env", None)
            )
            bootstrap = _build_chunked_bash_bootstrap(
                scratch_var, include_dir_bootstrap=needs_dir_bootstrap
            )
            argv = ["/bin/bash", "-c", bootstrap, "bash", *chunks]
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
                include_dir_bootstrap=needs_dir_bootstrap,
            )
        state = record.get("state")
        if state == "recovery_required":
            raise RuntimeError("Runfield execution requires recovery")
        raw_code = record.get("exit_code")
        code = (
            raw_code
            if isinstance(raw_code, int)
            and not isinstance(raw_code, bool)
            and state != "canceled"
            else 1
        )
        result = ExecResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            return_code=code,
        )
        if (
            needs_dir_bootstrap
            and state == "succeeded"
            and result.return_code == 0
        ):
            self._harbor_dirs_prepared = True
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
        timeout_sec: float | None,
        close_on_timeout: bool = True,
        priority: str | None = None,
        include_dir_bootstrap: bool = False,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        """Upload an oversized Bash program and execute it with bounded argv."""
        remote_path = f"/tmp/.harbor-command-{uuid.uuid4().hex}.sh"
        with tempfile.TemporaryDirectory(prefix="harbor-runfield-command-") as temporary:
            local_path = Path(temporary) / "command.sh"
            local_path.write_bytes(command)
            local_path.chmod(0o600)
            await self.upload_file(local_path, remote_path)
        # The script is owned by this Sandbox and reclaimed on close. Removing it
        # must not introduce a guest Python/shell dependency for host-side cleanup.
        argv = (
            [
                "/bin/bash",
                "-c",
                _MATERIALIZED_HARBOR_DIRS_BOOTSTRAP,
                "bash",
                remote_path,
            ]
            if include_dir_bootstrap
            else ["/bin/bash", remote_path]
        )
        return await self._execute_argv(
            argv,
            cwd=cwd,
            env=effective_env if (effective_env := env) is not None else None,
            timeout_sec=timeout_sec,
            close_on_timeout=close_on_timeout,
            priority=priority,
        )
