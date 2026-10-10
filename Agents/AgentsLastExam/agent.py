"""Harbor bridge for image-provided ALE agents on Linux and Windows."""

import os
import tempfile
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.models.task.config import TaskOS
from kubevirt_windows.agent import WindowsCommandAgent


class ALECommandAgent(BaseAgent):
    SUPPORTS_WINDOWS = True

    def __init__(self, *args, linux_command=None, agent_timeout_sec=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.linux_command = linux_command if linux_command is not None else os.environ.get("HARBOR_ALE_LINUX_AGENT_COMMAND", "")
        self.timeout = agent_timeout_sec
        self.windows_args = (args, {**kwargs, "agent_timeout_sec": agent_timeout_sec})
        self.windows = None
        if self.mcp_servers or self.skills_dir:
            raise ValueError("ALECommandAgent requires image-provided tools and skills")

    @staticmethod
    def name():
        return "ale-command"

    def version(self):
        return os.environ.get("HARBOR_ALE_AGENT_VERSION")

    async def setup(self, environment):
        if environment.os == TaskOS.WINDOWS:
            self.windows = WindowsCommandAgent(*self.windows_args[0], **self.windows_args[1])
            await self.windows.setup(environment)
        elif not self.linux_command.strip():
            raise ValueError("Set HARBOR_ALE_LINUX_AGENT_COMMAND to a Linux agent entrypoint")

    async def run(self, instruction, environment, context):
        if environment.os == TaskOS.WINDOWS:
            await self.windows.run(instruction, environment, context)
            return
        with tempfile.TemporaryDirectory(prefix="ale-agent-") as tmp:
            prompt = Path(tmp) / "instruction.txt"
            prompt.write_text(instruction, encoding="utf-8")
            script = Path(tmp) / "run.sh"
            script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + self.linux_command + "\n")
            await environment.upload_file(prompt, "/logs/agent/instruction.txt")
            await environment.upload_file(script, "/logs/agent/run.sh")
        with environment.scoped_exec_env({"HARBOR_INSTRUCTION_FILE": "/logs/agent/instruction.txt",
                                           "HARBOR_MODEL": self.model_name or ""}):
            result = await environment.exec(
                "bash /logs/agent/run.sh > /logs/agent/stdout.txt 2> /logs/agent/stderr.txt",
                timeout_sec=self.timeout)
        context.metadata = {**(context.metadata or {}), "exit_code": result.return_code}
        if result.return_code:
            raise RuntimeError(f"Linux agent exited with code {result.return_code}; see agent logs")
