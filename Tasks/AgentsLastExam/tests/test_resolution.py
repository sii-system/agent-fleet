"""Display setup must preserve the native ALE benchmark profile by default."""

import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ale_adapter.native import set_windows_resolution


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def resolution(self, *, code=0, stdout="set_ok"):
        sandbox = SimpleNamespace(python="C:/ale/python.exe", run_command=AsyncMock(
            return_value=SimpleNamespace(returncode=code, stdout=stdout)))
        modules = {"ale_run.environments.providers.gcloud": SimpleNamespace(_SET_RES_PY="fixture script")}
        with patch.dict(sys.modules, modules):
            return await set_windows_resolution(sandbox, [1024, 768])

    async def test_success_records_applied_profile(self):
        self.assertEqual(await self.resolution(), {"requested": [1024, 768], "applied": True})

    async def test_rejected_resolution_fails_unless_explicitly_relaxed(self):
        for setting in (None, "", "1", "true", "unexpected"):
            with patch.dict(os.environ, {}, clear=True):
                if setting is not None:
                    os.environ["ALE_WINDOWS_RESOLUTION_STRICT"] = setting
                for code, stdout in ((0, "failed:-2 supported=[(1280,800)]"), (1, "set_ok")):
                    with self.subTest(setting=setting, code=code), self.assertRaisesRegex(RuntimeError, "resolution"):
                        await self.resolution(code=code, stdout=stdout)

    async def test_diagnostic_override_records_deviation_and_redacts_output(self):
        with patch.dict(os.environ, {"ALE_WINDOWS_RESOLUTION_STRICT": "0"}), \
                patch("sys.stderr", new_callable=io.StringIO) as output:
            result = await self.resolution(stdout="fake-secret")
        self.assertEqual(result, {"requested": [1024, 768], "applied": False})
        self.assertIn("explicit diagnostic mode", output.getvalue())
        self.assertNotIn("fake-secret", output.getvalue())
