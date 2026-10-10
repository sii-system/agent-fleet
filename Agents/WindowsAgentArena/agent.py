"""Host-side WAA predict/reset agents; no Windows CLI installation required."""

from __future__ import annotations

import math
from importlib import import_module

from harbor.agents.base import BaseAgent


def pcagent_class(client=None):
    if client:
        from pathlib import Path

        import mm_agents

        path = str(Path(client) / "mm_agents")
        if path not in mm_agents.__path__:
            mm_agents.__path__.append(path)
    from mm_agents.pcagent.agent import PCAgent

    return PCAgent


def load_agent(spec, *, model, screenshot_size, pcagent_client=None):
    if spec == "pcagent":
        PCAgent = pcagent_class(pcagent_client)
        agent = PCAgent(model=model, screenshot_size=screenshot_size)
    else:
        if ":" not in spec:
            raise ValueError("Custom WAA agents use module:factory")
        module, name = spec.rsplit(":", 1)
        agent = getattr(import_module(module), name)(model=model, screenshot_size=screenshot_size)
    if not callable(getattr(agent, "reset", None)) or not callable(getattr(agent, "predict", None)):
        raise TypeError("WAA agents must provide reset() and predict(instruction, observation)")
    return agent


class WAAAgent(BaseAgent):
    """Harbor agent executing a predict/reset factory on its trial's desktop."""

    SUPPORTS_WINDOWS = True

    def __init__(self, *args, factory="pcagent", max_steps=15, pause=1.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.factory, self.max_steps, self.pause = factory, int(max_steps), float(pause)
        if self.max_steps < 1 or not math.isfinite(self.pause) or self.pause < 0:
            raise ValueError("Invalid WAA prediction budget or pause")
        if self.mcp_servers or self.skills_dir:
            raise ValueError("WAA predict/reset agents do not configure MCP servers or skills")

    @staticmethod
    def name():
        return "waa"

    def version(self):
        return "1"

    async def setup(self, environment):
        from waa_benchmark.environment import WAASession

        if not isinstance(environment, WAASession):
            raise TypeError("WAAAgent requires a WAA session")
        if self.factory == "pcagent" and environment.action_space != "pyautogui":
            raise ValueError("PC-Agent requires pyautogui action space")

    async def run(self, instruction, environment, context):
        result = await environment.native_request("agent", factory=self.factory, model=self.model_name,
                                                   instruction=instruction, max_steps=self.max_steps,
                                                   pause=self.pause, extra_env=self.extra_env)
        context.metadata = {**(context.metadata or {}), **result}
