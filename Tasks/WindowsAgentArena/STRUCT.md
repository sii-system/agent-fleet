# WindowsAgentArena Harbor adapter structure

| File | Owns |
| --- | --- |
| `setup.sh`, `prepare.py`, `source.py`, `preflight.py` | Explicit setup, pinned clients, full-manifest dependency checks |
| `dataset.py`, `adapter.py` | Release manifests, selection, generated Windows Harbor tasks |
| `run.sh`, `launch.py` | Config loading, task selection and direct exec of Harbor CLI |
| `environment.py` | Native setup/session attached to Harbor's trial VM |
| `verifier.py` | Native evaluate phase, reward artifact and Harbor VerifierResult |
| `worker.py`, `desktop.py` | Isolated native reset/predict/step/evaluate/record and endpoint routing |
| `../../Agents/WindowsAgentArena/agent.py` | Harbor BaseAgent and reference/custom factories |
| `../../Agents/utils/common/Harbor/kubevirt_windows/` | Cluster credentials, CDI clones, VM ownership and cleanup |

Harbor owns scheduling, phase timeouts, retries, persistence, reporting, and resume.
The environment owns the native subprocess; agent and verifier invoke separate
phases on the same desktop session. Workers do not manage cluster resources.
Prepared clients and generated tasks remain outside the repository. Agents receive
instructions and observations; evaluators retain the selected release's native config.
