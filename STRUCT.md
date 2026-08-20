# Repository Structure

Agent Fleet separates runnable agents and task assets so each part can be
used independently.

```text
agent-fleet/
├── Agents/
│   ├── Openclaw/              # Dockerized OpenClaw gateway fleet
│   ├── Harbor-claude-code/    # Claude Code tracing/integration code
│   ├── Harbor-opencode/       # OpenCode tracing/integration code
│   ├── Harbor-pi/             # Pi task-container integration code
│   └── utils/
│       ├── common/Harbor/     # Shared Harbor runner, zellij layout, workers
│       └── rl/                # Remote rollout listener, workers, and helpers
├── scripts/
│   ├── setup.sh               # Host setup orchestration entry point
│   ├── setup_config.py        # Setup config parsing and managed-file updates
│   ├── prerequisites.sh       # Shared managed-tool discovery/bootstrap
│   └── script_utils.py        # URL and checksum helpers for shell callers
├── Tasks/
│   ├── Pinchbench/            # PinchBench runner for OpenClaw
│   ├── clawBio/               # ClawBio runner for OpenClaw
│   ├── SWE-verify/            # SWE-bench Verified task list
│   ├── SWE-smith/             # SWE-Smith task list
│   ├── Terminal-bench-2/      # Terminal-Bench task lists
│   └── SETA/                  # SETA task lists
```

## Design

`Agents/` owns execution. Agent-specific code stays under its own directory, while shared Harbor orchestration lives under `Agents/utils/common/Harbor/`.

`Tasks/` owns benchmark and task inputs. Harbor and OpenClaw runners read task lists from here instead of duplicating task files inside agent directories.

Shell files remain the operator entry points and own top-level workflow
orchestration and environment setup. Focused Python modules own delegated
workflows such as structured parsing, file updates, archive handling, summary
rendering, and any commands those workflows require; shell entry points invoke
those modules rather than embedding Python programs.

## Cross-Directory Calls

Harbor common resolves the repository root from `Agents/utils/common/Harbor/env.sh`, then derives:

- `AGENTS_DIR=$REPO_ROOT/Agents`
- `TASKS_DIR=$REPO_ROOT/Tasks`
- `HARBOR_CLAUDE_CODE_DIR=$AGENTS_DIR/Harbor-claude-code`
- `HARBOR_OPENCODE_DIR=$AGENTS_DIR/Harbor-opencode`
- `HARBOR_PI_DIR=$AGENTS_DIR/Harbor-pi`

OpenClaw benchmark runners call `Agents/Openclaw` for fleet setup and Docker Compose, then use task-specific code under `Tasks/Pinchbench` or `Tasks/clawBio`.

Opik tracing code is linked as a Git submodule at
`third_party/agent-opik-plugin`, pinned by the repository gitlink.
