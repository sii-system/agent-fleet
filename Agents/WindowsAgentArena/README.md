# WindowsAgentArena Harbor agent

`Agents.WindowsAgentArena.agent:WAAAgent` implements Harbor's `BaseAgent` interface
for the [WAA and WAA-V2 task adapters](../../Tasks/WindowsAgentArena/README.md).
It supports Windows and requires `waa_benchmark.environment:WAAEnvironment`.
The native environment performs task setup; the agent runs predictions/actions;
`waa_benchmark.verifier:WAAVerifier` independently grades the resulting desktop.
Harbor owns the lifecycle and phase timeouts.

The default factory is the reference PC-Agent from pinned WAA-V2. It can drive
both releases, using an OpenAI-compatible vision chat-completions model. On
original WAA, this is an alternative to Microsoft's Navi baseline. No Windows
CLI installation is needed; the agent runs in the host trial subprocess.

Supply a custom agent with `--agent your_module:build` to the benchmark/fleet
launcher, or `--ak factory=your_module:build` to direct Harbor. The module must be
importable in the prepared host environment:

```python
def build(*, model, screenshot_size):
    return MyAgent(model=model, screenshot_size=screenshot_size)

class MyAgent:
    def reset(self):
        pass

    def predict(self, instruction, observation):
        # Native contract: response, nonempty action list, logs, computer update args.
        return "Finished", ["DONE"], {"plan_result": "Finished"}, None
```

Factories receive the model and actual screenshot dimensions. The agent receives
the instruction and observation, without evaluator configuration. `max_steps`
(default 15) counts predictions, and `pause` defaults to 1 second. Actions follow
the selected release's native `pyautogui`, `code_block`, or `computer_13` contract;
PC-Agent requires `pyautogui`. `WAIT`, `FAIL`, and `DONE` retain native semantics.
Use Harbor environment kwargs `--ek action_space=code_block` and `--ek a11y=true`
for other contracts. Pass them after `--` through the dedicated launcher. Custom
factories may use their own credentials/providers; the launcher
does not require shared gateway credentials for them. MCP servers and Harbor skills
are not configured by this predict/reset bridge.
