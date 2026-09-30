"""Agents' Last Exam Windows CUA /cmd SSE protocol."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import uuid
from pathlib import Path, PureWindowsPath

from .transport import WindowsTransport, encoded_powershell, ps_quote, windows_path

CHUNK_BYTES = 1024 * 1024


class ALETransport(WindowsTransport):
    protocol = "ALE"

    async def _cmd(self, command, params, *, timeout=60):
        raw = await self._request(
            "POST", "/cmd", timeout=timeout,
            json={"command": command, "params": params},
        )
        # CUA returns data: JSON even with Content-Type: text/plain. Ignore
        # SSE comments/metadata; support CRLF, BOM and multiline data fields.
        try:
            fields = []
            for line in raw.decode("utf-8-sig").splitlines() + [""]:
                if line.startswith("data:"):
                    fields.append(line[5:].removeprefix(" "))
                elif not line and fields:
                    result = json.loads("\n".join(fields))
                    if not isinstance(result, dict) or result.get("success") is not True:
                        raise RuntimeError(f"ALE {command} failed")
                    return result
        except ValueError:
            pass
        raise RuntimeError("Invalid ALE command response")

    async def probe(self):
        raw = await self._request("GET", "/status", timeout=15)
        try:
            if json.loads(raw).get("status") == "ok":
                return
        except (ValueError, AttributeError):
            pass
        raise RuntimeError("Unexpected ALE guest readiness response")

    async def _run_powershell(self, script, *, timeout):
        result = await self._cmd(
            "run_command", {"command":
                "powershell.exe -NoLogo -NoProfile -NonInteractive "
                "-ExecutionPolicy Bypass -EncodedCommand " + encoded_powershell(script)},
            timeout=timeout,
        )
        if type(result.get("return_code")) is not int or result["return_code"] != 0:
            raise RuntimeError("ALE PowerShell helper failed")
        return result.get("stdout")

    async def upload_file(self, source, target):
        source = Path(source).absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Upload requires a regular file: {source}")
        target = windows_path(target)
        stage = target + "." + uuid.uuid4().hex + ".upload"
        part = stage + ".part"
        # CUA write_bytes replaces a file. Write bounded chunks to a temporary
        # part, append in PowerShell, then publish the completed destination.
        async with asyncio.timeout(self.settings.transfer_timeout):
            await self.mkdir(str(PureWindowsPath(target).parent))
            await self.powershell(f"[IO.File]::WriteAllBytes({ps_quote(stage)}, [byte[]]@())")
            try:
                with source.open("rb") as data:
                    while chunk := data.read(CHUNK_BYTES):
                        await self._cmd("write_bytes", {
                            "path": part, "content_b64": base64.b64encode(chunk).decode("ascii"),
                        }, timeout=self.settings.transfer_timeout)
                        await self.powershell(
                            f"$src = [IO.File]::OpenRead({ps_quote(part)}); "
                            "try { "
                            f"$dst = [IO.File]::Open({ps_quote(stage)}, 'Append', 'Write'); "
                            "try { $src.CopyTo($dst) } finally { $dst.Dispose() } "
                            "} finally { $src.Dispose() }"
                        )
                await self.powershell(
                    f"Move-Item -LiteralPath {ps_quote(stage)} -Destination {ps_quote(target)} -Force"
                )
            finally:
                # Best effort: failures are still isolated to this trial's VM.
                try:
                    await self.powershell(
                        f"Remove-Item -LiteralPath {ps_quote(stage)}, {ps_quote(part)} "
                        "-Force -ErrorAction SilentlyContinue",
                        timeout=15,
                    )
                except (RuntimeError, TimeoutError):
                    pass

    async def download_file(self, source, target):
        source = windows_path(source)
        target = Path(target).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        async with asyncio.timeout(self.settings.transfer_timeout):
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as data:
                stage = Path(data.name)
                try:
                    offset = 0
                    while True:
                        result = await self._cmd("read_bytes", {
                            "path": source, "offset": offset, "length": CHUNK_BYTES,
                        }, timeout=self.settings.transfer_timeout)
                        try:
                            chunk = base64.b64decode(result["content_b64"], validate=True)
                        except (ValueError, TypeError, KeyError):
                            raise RuntimeError("Invalid ALE file response") from None
                        if len(chunk) > CHUNK_BYTES:
                            raise RuntimeError("ALE file response exceeded its chunk limit")
                        data.write(chunk)
                        offset += len(chunk)
                        if len(chunk) < CHUNK_BYTES:
                            break
                    data.close()
                    stage.replace(target)
                finally:
                    stage.unlink(missing_ok=True)
