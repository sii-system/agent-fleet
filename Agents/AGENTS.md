# AGENTS.md — Agents/

Execution code lives here: the shared **Harbor benchmark runner**
(`utils/common/Harbor/`), **remote rollout service** (`utils/rl/`),
agent integrations, and the **OpenClaw gateway fleet** (`Openclaw/`).
Repo-wide setup and config rules: [root AGENTS.md](../AGENTS.md).

## Harbor Benchmark Runner (`utils/common/Harbor/`)

Runs Claude Code, OpenCode, or Pi against Harbor datasets in parallel zellij
workers.

Prereq: run `./scripts/setup.sh` from the repo root and set model gateway
values in `config.local.env`. `OPIK_URL` enables tracing; initialize the
submodule for traced runs. Authless Opik endpoints do not require an API key.

Setup owns the pinned host runner environment; workload startup validates it
and must not install or repair it. Dependency pins live in
`utils/common/Harbor/runner-requirements.txt`. Keep setup, startup validation,
and DinD aligned on `HARBOR_RUNNER_PYTHON_VERSION`.

### Run

Use `./scripts/run_fleet.sh` for the unified launcher (see
[scripts/README.md](../scripts/README.md)). For direct Harbor runs:

```bash
cd Agents/utils/common/Harbor
export AGENT=claude-code DATASET_NAME=terminalbench21
export TOTAL_WORKERS=1 HARBOR_N_CONCURRENT=1
bash start.sh --detach     # detached zellij session (prints session name)
bash start.sh              # interactive zellij session
zellij attach <session-name>
```

Main `env.sh` parameters:

```bash
AGENT="claude-code"        # claude-code, opencode, or pi
DATASET_NAME="seta"        # registry alias, registry ID, or auto for local data
DATASET_PATH="/workspace/seta-env/Harbor-Dataset"  # local runs only
TOTAL_WORKERS="80"         # zellij worker panes
HARBOR_N_CONCURRENT="80"       # Harbor concurrency, normally = TOTAL_WORKERS
```

Outputs land in `OUTPUT_PATH` (default `<repo>/runs/<RUN_ID>`).
`RESET_RUN=1` clears run state (including online-analysis state) before
restarting.

### Datasets

| Dataset | `DATASET_NAME` | Typical `DATASET_PATH` | Workers |
| --- | --- | --- | --- |
| SETA | `seta` | `/workspace/seta-env/Harbor-Dataset` | 80 |
| SWE-Smith | `smith` | `/workspace/harbor/datasets/swesmith` | 80 |
| Terminal-Bench 2.1 | `terminalbench21` | `/workspace/terminal-bench-2-1/tasks` | 20 |
| SWE-bench Verified | `sweverify` | `/workspace/swebench-verified` | 20 |

`seta`, `terminalbench21`, and `sweverify` resolve to registry datasets by
default; `smith` stays local. Registry IDs such as `tmax/TMax-15K-Harbor`
also work. Registry runs bypass local task lists. Use `DATASET_NAME=auto`
with `DATASET_PATH` for local data; `TASK_SOURCE_FILE=<path>` overrides the
local task list under `Tasks/`. See [Tasks/AGENTS.md](../Tasks/AGENTS.md).

### Sandbox Backends

`RL_ENVIRONMENT_TYPE` selects `docker` (default), `e2b`, `qz`, or
`opensandbox`; fixed runs derive `HARBOR_ENVIRONMENT_TYPE` from it unless
explicitly overridden. Read the relevant backend contract before changing
image preparation, runtime delivery, or provider configuration:

- [E2B](utils/common/Harbor/E2B_README.md)
- [qz](utils/common/Harbor/QZ_SANDBOX_README.md) and
  [template management](utils/common/Harbor/QZ_TEMPLATE_MANAGER.md)
- [OpenSandbox](utils/common/Harbor/OPENSANDBOX_README.md) and
  [Bundle/image management](utils/common/Harbor/OPENSANDBOX_IMAGE_MANAGER.md)

Reusable Python runtime construction belongs in `python_runtime.py`;
dataset-specific verifier composition belongs in `verifier_runtime/`.

### Monitor, Analyzer, and Fixer

Fixed benchmark runs start the monitor and Pi-backed analyzer by default.
`HARBOR_MONITOR_ENABLED=0` disables monitoring;
`HARBOR_ANALYZER_ENABLED=0` disables only the analyzer. The analyzer uses
model gateway defaults or `HARBOR_ANALYZER_*` overrides.

Keep observation (`scripts/harbor_monitor/`), decisions/execution
(`scripts/harbor_controller/`), analysis (`scripts/harbor_analyzer/`), and
repair (`scripts/harbor_fixer/`) separate. Monitor observations do not
automatically restart or stop runs. Controller decisions are submitted through
`scripts/controller.py`; its Fixer workflow plans first, then binds approval
to the exact plan before execution and smoke verification. Preserve these
contracts when modifying automation.

Artifacts live under `$OUTPUT_PATH/{monitor,analyzer,fixer}`. Workflow commands
and artifact contracts: [Harbor README](utils/common/Harbor/README.md) and
[Analyzer architecture](utils/common/Harbor/ANALYZER_ARCHITECTURE.md).

### Online Analysis (opt-in)

```bash
HARBOR_ONLINE_ANALYSIS=1 bash start.sh --detach
```

Console-only diagnostics; events and summary land in
`<OUTPUT_PATH>/online-analysis/`. For SETA only, `HARBOR_EARLY_STOP=1`
additionally stops a worker's current task when a matching
`task_blocking=true` event appears.

### Scripts

| Script | Role |
| --- | --- |
| `start.sh` | Main launcher: validates `AGENT`, prepares output dirs and task file, starts zellij |
| `env.sh` | Path resolution and runtime defaults (sources repo `config.env` / `config.local.env`) |
| `gen_harbor_zellij_layout.sh` | Writes the zellij layout (monitor + worker panes) |
| `run_harbor_worker.sh` | Worker loop for one pane: claims tasks, calls `harboropik.sh` |
| `harboropik.sh` | Harbor CLI orchestration wrapper with Opik/tracing setup |
| `harbor_shell_utils.py` | Structured event, JSON, URL, and mount helpers called by Harbor shell wrappers |
| `monitor_harbor.sh` | Run monitor pane and delegate summary calculations |
| `harbor_monitor_utils.py` | Reward, success, exception, and environment summary calculations |
| `prepare_local_deps.sh` | Thin interpreter-selection wrapper for local dependency preparation |
| `prepare_local_deps.py` | Local package downloads, runtime archives, caches, and manifest generation |

Full variable table: [utils/common/Harbor/STRUCT.md](utils/common/Harbor/STRUCT.md).
Agent integration internals: [Harbor-claude-code/STRUCT.md](Harbor-claude-code/STRUCT.md),
[Harbor-opencode/STRUCT.md](Harbor-opencode/STRUCT.md),
[Harbor-pi/STRUCT.md](Harbor-pi/STRUCT.md).

## Remote Rollout (`utils/rl/`)

```bash
ROLLOUT=1 bash Agents/utils/common/Harbor/start.sh --detach
```

The shared launcher loads `utils/rl/RL-env.sh` (or `RL_ENV_FILE`) and starts
the listener instead of a fixed benchmark. `RL_DATASET_ROOTS` maps dataset
names to local paths; `RL_AGENT` selects the worker agent. The listener serves
`/health`, `/datasets`, and `/run_trial`, with per-submission queues and zellij
workers. Keep rollout implementation and `RL_*` defaults in `utils/rl/`.
Trusted host request configuration must not become caller-controlled through
`/run_trial` payloads. Full configuration and lifecycle:
[Harbor rollout docs](utils/common/Harbor/README.md#rl-rollout-mode).

## OpenClaw Fleet (`Openclaw/`)

N isolated OpenClaw gateway containers on one host; instance N listens on
port `18789 + (N-1)*20`.

### Build, Set Up, Launch

```bash
./Agents/Openclaw/scripts/build-openclaw-image.sh   # build image
# Opik-enabled image instead:
OPIK_URL=https://opik.example.com/api ./Agents/Openclaw/scripts/build-openclaw-image.sh

BASE_URL="https://api.example.com/v1" API_KEY="sk-fake" MODEL="your-model-id" \
  ./Agents/Openclaw/scripts/setup.sh 3              # generate 3 instances

docker compose -f Agents/Openclaw/docker-compose.yml up -d
```

`setup.sh` exits without generating anything if `BASE_URL` or `API_KEY` is
missing (values may also come from `config.local.env`). It writes the
generated files listed in the root AGENTS.md — regenerate them, never edit
by hand.

### Manage

```bash
./Agents/Openclaw/scripts/openclaw-fleet.sh status
./Agents/Openclaw/scripts/openclaw-fleet.sh logs all --tail 100
./Agents/Openclaw/scripts/openclaw-fleet.sh restart 1,3
./Agents/Openclaw/scripts/openclaw-fleet.sh scale 5
```

Commands: `status`, `probe`, `logs`, `start`, `stop`, `restart`, `token`,
`config`, `config-set`, `exec`, `workspace`, `clean-workspace`, `scale`,
`plugin-status`, `df`, `help`. Selectors: `all`, `3`, `1,3,5`, `2-5`.

### Session TUI

```bash
./Agents/Openclaw/scripts/start-session-tui.sh
```

zellij grid with a fleet monitor pane plus per-instance session panes
(`gen_session_zellij_layout.sh`, `monitor_openclaw_sessions.sh`, and
`stream_openclaw_session.sh` are its layout/monitor/stream helpers).

Multi-node deployment (Ansible), the full variable reference, and
`openclaw.json.template`: [Openclaw/GUIDE.md](Openclaw/GUIDE.md).
Security policy: [Openclaw/SECURITY.md](Openclaw/SECURITY.md).

## Development

Run the affected suites from the repo root with Python 3.12 and the test
dependencies in `utils/common/Harbor/runner-requirements.txt`, as in portable
CI. Set `PYTHONPATH=.` for repository imports:

```bash
export PYTHONPATH=.
python3 -m unittest discover -s Agents/utils/common/Harbor/tests
python3 -m unittest discover -s Agents/utils/rl/tests
python3 -m unittest discover -s Agents/Harbor-claude-code/tests
python3 -m unittest discover -s Agents/Harbor-opencode/tests
python3 -m unittest discover -s Agents/Harbor-pi/tests
python3 -m unittest discover -s Agents/Openclaw/tests
bash Agents/Openclaw/tests/test_build_openclaw_image.sh
bash Agents/Openclaw/tests/test_session_layout.sh
bash Agents/Openclaw/tests/test_start_session_tui.sh
bash Agents/Openclaw/tests/test_stream_openclaw_session_sh.sh
```

Harbor and rollout also have `tests/test_*.sh` regressions; run the scripts
covering the changed behavior with `bash`. Backend smoke runs need their
provider infrastructure; unit tests alone do not establish backend health.
