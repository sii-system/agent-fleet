"""Runtime preparation through Harbor's Windows command agent."""

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import TaskOS
from kubevirt_windows.agent import WindowsCommandAgent


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifact = self.root / "tool.zip"
        self.artifact.write_bytes(b"versioned portable tool")
        self.spec = {
            "version": 1,
            "files": [
                {
                    "source": "tool.zip",
                    "target": "C:/tools/tool.zip",
                    "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
                }
            ],
            "commands": ["prepare-tools.cmd"],
            "env": {"TOOL_HOME": "C:/tools/runtime"},
        }
        self.manifest = self.root / "prepare.json"
        self.environment = AsyncMock(spec=BaseEnvironment)
        self.environment.os = TaskOS.WINDOWS
        self.environment.exec.return_value = ExecResult(
            return_code=0, stdout="", stderr=""
        )

    def agent(self, **kwargs):
        self.manifest.write_text(json.dumps(self.spec))
        return WindowsCommandAgent(
            logs_dir=self.root / "logs",
            command="tool.exe",
            prepare_manifest=str(self.manifest),
            check_command="tool.exe --version",
            **kwargs,
        )

    async def test_prepare_uploads_pinned_artifact_before_command_and_readiness(self):
        agent = self.agent()
        uploaded = []

        async def upload(source, target):
            uploaded.append((Path(source).read_bytes(), target))

        self.environment.upload_file.side_effect = upload
        await agent.setup(self.environment)
        self.assertEqual(uploaded, [(self.artifact.read_bytes(), "C:/tools/tool.zip")])
        calls = self.environment.exec.await_args_list
        self.assertEqual(
            [c.args[0] for c in calls], ["prepare-tools.cmd", "tool.exe --version"]
        )
        self.assertEqual(calls[0].kwargs["env"], self.spec["env"])
        self.assertEqual(calls[1].kwargs["env"], self.spec["env"])

    async def test_checksum_failure_prevents_all_guest_changes(self):
        agent = self.agent()
        self.artifact.write_bytes(b"unexpected version")
        with self.assertRaisesRegex(ValueError, "checksum"):
            await agent.setup(self.environment)
        self.environment.upload_file.assert_not_awaited()
        self.environment.exec.assert_not_awaited()

    async def test_preparation_failure_does_not_run_readiness(self):
        agent = self.agent()
        self.environment.exec.return_value = ExecResult(
            return_code=9, stdout="", stderr=""
        )
        with self.assertRaisesRegex(RuntimeError, "preparation"):
            await agent.setup(self.environment)
        self.assertEqual(self.environment.exec.await_count, 1)

    async def test_preparation_deadline_covers_uploads(self):
        agent = self.agent(prepare_timeout_sec=0.01)

        async def blocked(*args):
            await asyncio.Event().wait()

        self.environment.upload_file.side_effect = blocked
        with self.assertRaises(TimeoutError):
            await agent.setup(self.environment)
        self.environment.exec.assert_not_awaited()

    async def test_runtime_env_and_provenance_survive_into_agent_run(self):
        agent = self.agent()
        context = AgentContext()
        await agent.setup(self.environment)
        await agent.run("task instruction", self.environment, context)
        overlay = self.environment.scoped_exec_env.call_args.args[0]
        self.assertEqual(
            self.environment.exec.call_args.kwargs["env"]["TOOL_HOME"], "C:/tools/runtime"
        )
        self.assertEqual(
            overlay["HARBOR_INSTRUCTION_FILE"], "C:\\logs\\agent\\instruction.txt"
        )
        self.assertIn("preparation_sha256", context.metadata)
        self.assertNotIn("TOOL_HOME", json.dumps(context.metadata))

    def test_rejects_bad_manifest_before_start(self):
        original = self.spec.copy()
        for update in [
            {"version": 2},
            {"commands": "not-a-list"},
            {"env": {"KEY": 123}},
            {"unknown": "typo"},
            {"files": [{"source": "tool.zip", "target": "relative", "sha256": "x"}]},
        ]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.spec = {**original, **update}
                self.agent()
