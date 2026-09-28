"""Harbor bridge for an operator-provisioned Windows agent entrypoint."""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.models.task.config import TaskOS

from .preparation import WindowsPreparation


class WindowsCommandAgent(BaseAgent):
    SUPPORTS_WINDOWS = True

    def __init__(
        self,
        *args,
        command=None,
        check_command=None,
        agent_timeout_sec=None,
        prepare_manifest=None,
        prepare_timeout_sec=600,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.command = (
            command
            if command is not None
            else os.environ.get("HARBOR_WINDOWS_AGENT_COMMAND", "")
        )
        self.check_command = (
            check_command
            if check_command is not None
            else os.environ.get("HARBOR_WINDOWS_AGENT_CHECK_COMMAND", "")
        )
        self.timeout = agent_timeout_sec
        manifest = (
            prepare_manifest
            if prepare_manifest is not None
            else os.environ.get("HARBOR_WINDOWS_PREPARE_MANIFEST", "")
        )
        self.preparation = WindowsPreparation(manifest) if manifest else None
        self.prepare_timeout = float(prepare_timeout_sec)
        if not math.isfinite(self.prepare_timeout) or self.prepare_timeout <= 0:
            raise ValueError("Preparation timeout must be positive and finite")
        if not self.command.strip():
            raise ValueError(
                "Set HARBOR_WINDOWS_AGENT_COMMAND to a Windows agent entrypoint"
            )
        if self.mcp_servers or self.skills_dir:
            raise ValueError(
                "WindowsCommandAgent does not configure MCP servers or skills"
            )

    @staticmethod
    def name():
        return "windows-command"

    def version(self):
        return os.environ.get("HARBOR_WINDOWS_AGENT_VERSION")

    async def setup(self, environment):
        if environment.os != TaskOS.WINDOWS:
            raise ValueError("WindowsCommandAgent requires a Windows environment")
        if self.preparation:
            await self.preparation.apply(environment, self.prepare_timeout)
        if self.check_command:
            result = await environment.exec(
                self.check_command,
                env=self.preparation.env if self.preparation else {},
                timeout_sec=60,
            )
            if result.return_code:
                raise RuntimeError("Windows agent readiness command failed")

    async def run(self, instruction, environment, context):
        with tempfile.TemporaryDirectory(prefix="windows-agent-") as tmp:
            prompt = Path(tmp) / "instruction.txt"
            prompt.write_text(instruction, encoding="utf-8")
            script = Path(tmp) / "run.cmd"
            script.write_bytes(
                ("@echo off\r\n" + self.command + "\r\n").encode("utf-8")
            )
            await environment.upload_file(prompt, "C:/logs/agent/instruction.txt")
            await environment.upload_file(script, "C:/logs/agent/run.cmd")
        # Instructions never become shell syntax. The prepared entrypoint reads
        # this UTF-8 file and can use HARBOR_MODEL to select a configured model.
        env = {
            "HARBOR_INSTRUCTION_FILE": "C:\\logs\\agent\\instruction.txt",
            "HARBOR_MODEL": self.model_name or "",
        }
        with environment.scoped_exec_env(env):
            result = await environment.exec(
                "call C:\\logs\\agent\\run.cmd > C:\\logs\\agent\\stdout.txt 2> C:\\logs\\agent\\stderr.txt",
                env=self.preparation.env if self.preparation else {},
                timeout_sec=self.timeout,
            )
        context.metadata = {**(context.metadata or {}), "exit_code": result.return_code}
        if self.preparation:
            context.metadata["preparation_sha256"] = self.preparation.digest
        if result.return_code:
            raise RuntimeError(
                f"Windows agent exited with code {result.return_code}; see agent/stdout.txt and agent/stderr.txt"
            )
