"""Check guest directory permissions before the CUA helper is uploaded."""

import asyncio
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from ale_adapter.cua_proxy import CUAProxy
from harbor.environments.base import ExecResult


class ProxyPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_directories_are_writable_by_the_guest_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"

            async def execute(command, **kwargs):
                command = command.replace("/logs/", shlex.quote(str(logs)) + "/")
                process = await asyncio.create_subprocess_exec(
                    "bash", "-c", "umask 022; " + command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                return ExecResult(return_code=process.returncode,
                                  stdout=stdout.decode(), stderr=stderr.decode())

            backend = Mock(exec=AsyncMock(side_effect=execute),
                           upload_file=AsyncMock(side_effect=RuntimeError("stop before upload")))
            proxy = CUAProxy(backend, {})
            proxy.guest_dir = str(root / "cua scratch")
            with self.assertRaisesRegex(RuntimeError, "stop before upload"):
                await proxy.start()
            # The real guest creates these as root, then uses the image user.
            for directory in [Path(proxy.guest_dir), *(logs / name for name in ("agent", "verifier", "artifacts"))]:
                with self.subTest(directory=directory.name):
                    self.assertEqual(directory.stat().st_mode & (stat.S_IWOTH | stat.S_IXOTH),
                                     stat.S_IWOTH | stat.S_IXOTH)
