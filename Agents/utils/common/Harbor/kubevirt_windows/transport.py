"""Windows OpenSSH transport with explicit cmd.exe execution semantics."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import tempfile
import uuid
from pathlib import Path, PureWindowsPath

from .control import run_process


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


def sftp_quote(value):
    if any(c in str(value) for c in "\r\n\x00"):
        raise ValueError("Invalid SFTP path")
    value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    for char in "*?[":
        value = value.replace(char, "\\" + char)
    return '"' + value + '"'


class WindowsSSH:
    def __init__(self, settings, name, ip, local_dir):
        self.settings, self.name, self.ip = settings, name, ip
        if not ip:
            raise ValueError("ip is required")
        self.local_dir = Path(local_dir)
        self.remote_root = f"C:/ProgramData/AgentFleet/{name}"

    def options(self):
        return [
            "-F",
            "/dev/null",
            "-i",
            str(self.settings.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.local_dir / 'known_hosts'}",
            "-o",
            # Direct-IP connect: HostName overrides the destination host, so
            # the `self.name` argument in powershell()/sftp() resolves to the
            # VM ipAddress. A fresh known_hosts per trial pins the host key
            # for this VM only.
            f"HostName={self.ip}",
            "-o",
            f"User={self.settings.ssh_user}",
            "-p",
            str(self.settings.ssh_port),
        ]

    async def powershell(self, script, *, timeout=60):
        # The encoded command contains no shell metacharacters and works with
        # both Windows OpenSSH default shells (cmd.exe and PowerShell).
        encoded = base64.b64encode(
            ("$ErrorActionPreference='Stop'; " + script).encode("utf-16-le")
        ).decode()
        raw = await run_process(
            [
                "ssh",
                *self.options(),
                self.name,
                "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
                + encoded,
            ],
            timeout=timeout,
        )
        return raw.decode("utf-8-sig")

    async def sftp(self, lines):
        await run_process(
            ["sftp", *self.options(), "-b", "-", self.name],
            data=("\n".join(lines) + "\n").encode(),
            timeout=self.settings.transfer_timeout,
        )

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
        result = await self.powershell(
            "[Console]::Write('agent-fleet-ready')", timeout=15
        )
        if result.strip() != "agent-fleet-ready":
            raise RuntimeError("Unexpected Windows guest readiness response")

    async def execute(self, command, *, cwd, env, timeout):
        request = self.remote_root + "/" + uuid.uuid4().hex + ".json"
        payload = {
            "command": command,
            "cwd": windows_path(cwd),
            "env": env,
            "timeout_sec": timeout,
            "max_output_bytes": 1024 * 1024,
        }
        script = (
            f"& {ps_quote(self.remote_root + '/execute.ps1')} "
            f"-Request {ps_quote(request)}"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="kubevirt-exec-") as tmp:
                local = Path(tmp) / "request.json"
                local.write_text(json.dumps(payload), encoding="utf-8")
                await self.upload_file(local, request)
            raw = await self.powershell(script, timeout=timeout + 30)
            result = json.loads(raw)
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
            except (Exception, asyncio.CancelledError):
                # Consume completion even if the outer task is cancelled again.
                cleanup.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                logging.getLogger(__name__).exception(
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
        await self.sftp([f"put {sftp_quote(source)} {sftp_quote(target)}"])

    async def download_file(self, source, target):
        source = windows_path(source)
        target = Path(target).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.sftp([f"get {sftp_quote(source)} {sftp_quote(target)}"])

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
[Console]::Write((ConvertTo-Json -InputObject @($files.ToArray()) -Compress))
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
