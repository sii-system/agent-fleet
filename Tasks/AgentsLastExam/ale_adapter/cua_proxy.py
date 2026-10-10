"""Connect native ALE's CUA client to an existing Harbor Linux sandbox."""

import asyncio
import base64
import json
import secrets
import shlex
import tempfile
from pathlib import Path

from aiohttp import web


class CUAProxy:
    def __init__(self, backend, profile):
        self.backend = backend
        self.python = profile.get("guest_python", "/opt/ale-run/.venv/bin/python")
        self.guest_url = f"http://127.0.0.1:{int(profile.get('cua_port', 5000))}"
        self.token = secrets.token_hex(24)
        self.guest_dir = "/tmp/ale-cua-" + self.token
        self.runner = None
        self.profile = profile

    async def start(self):
        result = await self.backend.exec(f"mkdir -p -- {shlex.quote(self.guest_dir)} /logs/agent /logs/verifier /logs/artifacts",
                                         cwd="/", user="root", timeout_sec=60)
        if result.return_code:
            raise RuntimeError("Failed to prepare ALE sandbox directories")
        await self.backend.upload_file(Path(__file__).with_name("guest_http.py"), self.guest_dir + "/forward.py")
        if self.profile.get("startup_command"):
            result = await self.backend.exec(self.profile["startup_command"], cwd="/", user="root", timeout_sec=60)
            if result.return_code:
                raise RuntimeError("ALE Linux startup command failed")
        async with asyncio.timeout(float(self.profile.get("ready_timeout_sec", 120))):
            while True:
                try:
                    metadata, body = await self.forward("POST", "/cmd",
                        json.dumps({"command": "run_command", "params": {"command": "echo ok"}}).encode(),
                        {"Content-Type": "application/json"}, timeout=5)
                    data = next(json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: "))
                    if metadata["status"] == 200 and data.get("success") and not data.get("return_code", 0):
                        break
                except (RuntimeError, OSError, ValueError, StopIteration):
                    pass
                await asyncio.sleep(2)
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def forward(self, method, path, body=b"", headers=None, timeout=3600):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid CUA request path")
        name = secrets.token_hex(12)
        remote = self.guest_dir + "/" + name
        with tempfile.TemporaryDirectory(prefix="ale-cua-") as tmp:
            root = Path(tmp)
            payload = root / "request.json"
            payload.write_text(json.dumps({"url": self.guest_url + path, "method": method,
                "body": base64.b64encode(body).decode(), "headers": headers or {}, "timeout": timeout}))
            try:
                await self.backend.upload_file(payload, remote + ".request")
                result = await self.backend.exec(shlex.join([self.python, self.guest_dir + "/forward.py",
                    remote + ".request", remote]), cwd="/", timeout_sec=timeout + 10)
                if result.return_code:
                    raise RuntimeError("ALE CUA server is unavailable; check the prepared image/template")
                await self.backend.download_file(remote + ".json", root / "response.json")
                await self.backend.download_file(remote + ".body", root / "response.body")
                return json.loads((root / "response.json").read_text()), (root / "response.body").read_bytes()
            finally:
                await self.backend.exec("rm -f -- " + shlex.join([remote + suffix for suffix in (".request", ".json", ".body")]),
                                        cwd="/", timeout_sec=30)

    async def handle(self, request):
        headers = {key: request.headers[key] for key in ("Content-Type", "Accept") if key in request.headers}
        try:
            metadata, body = await self.forward(request.method, "/" + request.match_info["path"] +
                ("?" + request.query_string if request.query_string else ""), await request.read(), headers)
        except (RuntimeError, OSError, ValueError):
            raise web.HTTPBadGateway(text="ALE guest CUA request failed") from None
        return web.Response(status=metadata["status"], body=body, headers={"Content-Type": metadata["content_type"]})

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
