"""Shared guest execution, file transfer and log recovery for Windows backends."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path, PureWindowsPath

from harbor.environments.base import ExecResult
from harbor.utils.path_filter import filter_paths_by_patterns
from kubevirt_windows.transport import windows_path


class WindowsGuestIO:
    async def _preserve_logs(self):
        if not self._started or self.transport is None or self._log_snapshot is not None:
            return
        try:
            self._log_snapshot = tempfile.TemporaryDirectory(prefix="harbor-windows-logs-")
            root = Path(self._log_snapshot.name)
            for name in ("agent", "verifier", "artifacts"):
                (root / name).mkdir()
            # Bound the entire snapshot, including a guest that stopped responding.
            async with asyncio.timeout(self.settings.transfer_timeout):
                for name in ("agent", "verifier", "artifacts"):
                    try:
                        await self.download_dir(f"C:/logs/{name}", root / name)
                    except Exception:
                        self.logger.exception("Failed to preserve Windows %s logs before VM cleanup", name)
        except TimeoutError:
            self.logger.warning("Windows log snapshot timed out; continuing VM cleanup")
        except Exception:
            self.logger.exception("Failed to preserve Windows logs; continuing VM cleanup")

    def _cached_log_path(self, source_path):
        if self._started or self._log_snapshot is None:
            return None
        try:
            relative = PureWindowsPath(windows_path(source_path)).relative_to("C:/logs")
        except ValueError:
            return None
        return Path(self._log_snapshot.name).joinpath(*relative.parts)

    def _guest(self):
        if not self._started or self.transport is None:
            raise RuntimeError("Windows VM has not started")
        return self.transport

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        effective_user = user if user is not None else self.default_user
        if effective_user is not None:
            raise ValueError("Windows commands run as the guest server user; user selection is unsupported")
        timeout = self.settings.command_timeout if timeout_sec is None else timeout_sec
        if timeout <= 0:
            raise ValueError("Command timeout must be positive")
        merged = self._merge_env(env) or {}
        for key, value in merged.items():
            if not key or any(c in key for c in "=\x00") or "\x00" in value:
                raise ValueError("Invalid Windows environment variable")
        result = await self._guest().execute(
            command,
            cwd=cwd or self.task_env_config.workdir or "C:/workspace",
            env=merged,
            timeout=timeout,
        )
        output = ExecResult(
            stdout=result["stdout"],
            stderr=result["stderr"],
            return_code=result["return_code"],
        )
        callback = self._output_callback()
        if callback:
            for stream in ("stdout", "stderr"):
                if getattr(output, stream):
                    await callback(getattr(output, stream), stream)
        return output

    async def upload_file(self, source_path, target_path):
        await self._guest().upload_file(source_path, target_path)

    async def upload_dir(self, source_dir, target_dir):
        await self._guest().upload_dir(source_dir, target_dir)

    async def download_file(self, source_path, target_path):
        cached = self._cached_log_path(source_path)
        if cached is not None:
            target = Path(target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, target)
        else:
            try:
                await self._guest().download_file(source_path, target_path)
            except BaseException:
                # Do not present an interrupted snapshot transfer as a complete file.
                if self._log_snapshot is not None and Path(target_path).is_relative_to(self._log_snapshot.name):
                    Path(target_path).unlink(missing_ok=True)
                raise

    async def download_dir(self, source_dir, target_dir):
        await self.download_dir_filtered(source_dir=source_dir, target_dir=target_dir)

    async def download_dir_with_exclusions(self, *, source_dir, target_dir, exclude):
        await self.download_dir_filtered(
            source_dir=source_dir, target_dir=target_dir, exclude=exclude
        )

    async def download_dir_filtered(
        self, *, source_dir, target_dir, include=None, exclude=None, protect=None
    ):
        cached = self._cached_log_path(source_dir)
        if cached is not None:
            if not cached.is_dir():
                raise FileNotFoundError(source_dir)
            paths = [path.relative_to(cached).as_posix() for path in sorted(cached.rglob("*")) if path.is_file()]
        else:
            paths = await self._guest().list_files(source_dir)
        selected = filter_paths_by_patterns(paths, include=include, exclude=exclude)
        selected = list(
            dict.fromkeys(selected + [p for p in paths if p in (protect or [])])
        )
        target = Path(target_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        for path in selected:
            local = target / path
            if not local.resolve().is_relative_to(target):
                raise ValueError("Download would escape the output directory")
            await self.download_file(
                windows_path(source_dir).rstrip("/") + "/" + path, local
            )
