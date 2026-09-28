"""WAA guest HTTP transport with supervised cmd.exe execution."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import re
import tempfile
import uuid
from pathlib import Path, PureWindowsPath

import httpx


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def windows_path(value):
    value = str(value)
    path = PureWindowsPath(value)
    if not re.match(r"^[a-zA-Z]:[/\\]", value) or any(
        c in value[2:] for c in '\x00\r\n:*?"<>|'
    ):
        raise ValueError(f"Expected an absolute Windows drive path: {value!r}")
    if ".." in path.parts:
        raise ValueError("Parent traversal is unsupported")
    return path.as_posix()


def encoded_powershell(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class WAATransport:
    def __init__(self, settings, name, ip, local_dir):
        self.settings, self.name = settings, name
        address = ipaddress.ip_address(ip)
        host = f"[{address}]" if address.version == 6 else str(address)
        self.local_dir = Path(local_dir)
        self.remote_root = f"C:/ProgramData/AgentFleet/{name}"
        # WAA's guest service has no auth. Never send the platform token or
        # route guest requests through a controller-side HTTP proxy.
        self.client = httpx.AsyncClient(
            base_url=f"http://{host}:{settings.waa_port}", trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, *, timeout=60, target=None, **kwargs):
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream(method, path, timeout=timeout, **kwargs) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"WAA {path} failed (HTTP {response.status_code})")
                    if target is not None:
                        with target.open("wb") as output:
                            async for chunk in response.aiter_bytes(65536):
                                output.write(chunk)
                        return b""
                    output = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        if len(output) + len(chunk) > 16 * 1024 * 1024:
                            raise RuntimeError("WAA control response exceeded its output limit")
                        output.extend(chunk)
                    return bytes(output)
        except httpx.HTTPError:
            # Response bodies and commands may contain guest credentials.
            raise RuntimeError(f"WAA {path} transport failed") from None

    async def powershell(self, script, *, timeout=60):
        # WAA subprocess.run(text=True) uses the guest Python locale. Emit ASCII
        # base64 so Unicode paths/results survive even a non-UTF-8 Windows locale.
        wrapped = (
            "$ErrorActionPreference='Stop'; try { $value = & { " + script +
            " } | Out-String; [Console]::Write([Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes([string]$value))) } catch { exit 1 }"
        )
        raw = await self._request(
            "POST", "/execute", timeout=min(timeout, 90),
            json={"command": [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_powershell(wrapped),
            ], "shell": False},
        )
        try:
            result = json.loads(raw)
            if result["status"] != "success" or result["returncode"] != 0:
                raise RuntimeError("WAA PowerShell helper failed")
            return base64.b64decode(result["output"].strip(), validate=True).decode("utf-8-sig")
        except (ValueError, TypeError, KeyError):
            raise RuntimeError("Invalid WAA execution response") from None

    async def mkdir(self, path):
        await self.powershell(
            f"[void][IO.Directory]::CreateDirectory({ps_quote(windows_path(path))})"
        )

    async def prepare(self):
        self.local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self.mkdir(self.remote_root)
        await self.upload_file(
            Path(__file__).with_name("execute.ps1"), self.remote_root + "/execute.ps1"
        )

    async def probe(self):
        raw = await self._request("GET", "/probe", timeout=15)
        try:
            if json.loads(raw).get("status") != "Probe successful":
                raise ValueError
        except (ValueError, AttributeError):
            raise RuntimeError("Unexpected WAA guest readiness response") from None

    async def execute(self, command, *, cwd, env, timeout):
        request = self.remote_root + "/" + uuid.uuid4().hex + ".json"
        payload = {
            "command": command,
            "cwd": windows_path(cwd),
            "env": env,
            "timeout_sec": timeout,
            "max_output_bytes": 1024 * 1024,
        }
        result_path = request + ".result"
        script = (
            "$ErrorActionPreference='Stop'; try { $result = & "
            f"{ps_quote(self.remote_root + '/execute.ps1')} -Request {ps_quote(request)}; "
            "} catch { $result = '{\"error\":\"supervisor failed\"}' }; "
            f"[IO.File]::WriteAllText({ps_quote(result_path + '.tmp')}, [string]$result, "
            "(New-Object Text.UTF8Encoding($false))); "
            f"Move-Item -LiteralPath {ps_quote(result_path + '.tmp')} -Destination {ps_quote(result_path)}"
        )
        try:
            async with asyncio.timeout(timeout + 30):
                with tempfile.TemporaryDirectory(prefix="kubevirt-exec-") as tmp:
                    local = Path(tmp) / "request.json"
                    local.write_text(json.dumps(payload), encoding="utf-8")
                    await self.upload_file(local, request)
                # WAA caps each /execute at 120s. Start a detached supervisor,
                # then use short requests to poll its atomically published result.
                await self.powershell(
                    "Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList "
                    "'-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
                    + encoded_powershell(script) + "'",
                    timeout=30,
                )
                while True:
                    raw = await self.powershell(
                        f"if (Test-Path -LiteralPath {ps_quote(result_path)}) {{ "
                        f"[IO.File]::ReadAllText({ps_quote(result_path)}) }} else {{ 'null' }}",
                        timeout=30,
                    )
                    result = json.loads(raw)
                    if result is not None:
                        if not isinstance(result, dict) or "error" in result:
                            raise RuntimeError("WAA command supervisor failed")
                        break
                    await asyncio.sleep(1)
        except BaseException:
            # A marker handles cancellation before and after child creation.
            # The remote supervisor kills the whole cmd.exe process tree.
            cleanup = asyncio.create_task(
                self.powershell(
                    f"[IO.File]::WriteAllText({ps_quote(request + '.cancel')},'cancel')",
                    timeout=15,
                )
            )
            try:
                await asyncio.shield(cleanup)
            except (RuntimeError, TimeoutError, asyncio.CancelledError):
                # Consume completion even if the outer task is cancelled again.
                cleanup.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                logging.getLogger(__name__).warning(
                    "Remote cancellation could not be confirmed for VM %s; VM teardown is required",
                    self.name,
                )
            raise
        if result.get("timed_out"):
            raise TimeoutError(
                f"Windows command exceeded {timeout}s; process tree terminated"
            )
        return result

    async def upload_file(self, source, target):
        source = Path(source).absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Upload requires a regular file: {source}")
        target = windows_path(target)
        await self.mkdir(str(PureWindowsPath(target).parent))
        with source.open("rb") as data:
            await self._request(
                "POST", "/setup/upload", data={"file_path": target},
                files={"file_data": (source.name, data, "application/octet-stream")},
                timeout=self.settings.transfer_timeout,
            )

    async def download_file(self, source, target):
        source = windows_path(source)
        target = Path(target).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._request(
            "POST", "/file", data={"file_path": source}, target=target,
            timeout=self.settings.transfer_timeout,
        )

    async def upload_dir(self, source, target):
        source, target = Path(source), windows_path(target)
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Upload requires a regular directory")
        await self.mkdir(target)
        for item in sorted(source.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"Symlink uploads are unsupported: {item}")
            remote = target.rstrip("/") + "/" + item.relative_to(source).as_posix()
            if item.is_dir():
                await self.mkdir(remote)
            else:
                await self.upload_file(item, remote)

    async def list_files(self, source):
        source = windows_path(source)
        # Never traverse junctions/reparse points. JSON preserves Unicode and
        # filenames containing newlines instead of parsing ls output.
        script = f"""
$root = [IO.Path]::GetFullPath({ps_quote(source)}).TrimEnd('\\')
if (-not [IO.Directory]::Exists($root)) {{ throw 'Source directory is missing' }}
if ((Get-Item -LiteralPath $root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {{ throw 'Reparse point source is unsupported' }}
$pending = New-Object 'Collections.Generic.Stack[string]'
$pending.Push($root)
$files = New-Object 'Collections.Generic.List[string]'
while ($pending.Count) {{
    foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {{
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {{ continue }}
        if ($item.PSIsContainer) {{ $pending.Push($item.FullName) }}
        else {{ $files.Add($item.FullName.Substring($root.Length + 1).Replace('\\','/')) }}
    }}
}}
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
ConvertTo-Json -InputObject @($files.ToArray()) -Compress
"""
        paths = json.loads(
            await self.powershell(script, timeout=self.settings.transfer_timeout)
        )
        if not isinstance(paths, list):
            raise TypeError("Expected a list of guest file paths")
        for path in paths:
            # Guest-controlled paths must never escape the host output root.
            if (
                not isinstance(path, str)
                or not path
                or "\\" in path
                or PureWindowsPath(path).drive
                or path.startswith("/")
                or ".." in PureWindowsPath(path).parts
            ):
                raise ValueError("Invalid relative path returned by Windows guest")
            windows_path(source.rstrip("/") + "/" + path)
        return paths
