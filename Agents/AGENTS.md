# AGENTS.md — Agents/

Two runnable subsystems live here: the shared **Harbor benchmark runner**
(`utils/common/Harbor/`) and the **OpenClaw gateway fleet** (`Openclaw/`).
Repo-wide setup and config rules: [root AGENTS.md](../AGENTS.md).

## Harbor Benchmark Runner (`utils/common/Harbor/`)

Runs Claude Code, OpenCode, or Pi against Harbor datasets in parallel zellij
workers.

Prereq: model gateway values set in `config.local.env`. For traced runs only,
also initialize the submodule and configure Opik values.

### Run

```bash
cd Agents/utils/common/Harbor
vim env.sh                 # set the run parameters below
bash start.sh --detach     # detached zellij session (prints session name)
bash start.sh              # interactive zellij session
zellij attach <session-name>
```

Main `env.sh` parameters:

```bash
AGENT="claude-code"        # claude-code, opencode, or pi
DATASET_NAME="seta"        # deepsearchqa, seta, smith, terminalbench21, or sweverify
DATASET_PATH="/workspace/seta-env/Harbor-Dataset"
TOTAL_WORKERS="80"         # zellij worker panes
HARBOR_N_CONCURRENT="80"       # Harbor concurrency, normally = TOTAL_WORKERS
```

Outputs land in `OUTPUT_PATH` (default `<repo>/runs/<RUN_ID>`).
`RESET_RUN=1` clears run state (including online-analysis state) before
restarting.

### Datasets

| Dataset | `DATASET_NAME` | Typical `DATASET_PATH` | Workers |
| --- | --- | --- | --- |
| DeepSearchQA | `deepsearchqa` | Harbor registry | runner concurrency |
| SETA | `seta` | `/workspace/seta-env/Harbor-Dataset` | 80 |
| SWE-Smith | `smith` | `/workspace/harbor/datasets/swesmith` | 80 |
| Terminal-Bench 2.1 | `terminalbench21` | `/workspace/terminal-bench-2-1/tasks` | 20 |
| SWE-bench Verified | `sweverify` | `/workspace/swebench-verified` | 20 |

`DATASET_NAME` selects a task list under `Tasks/` (for example
`Tasks/SETA/harbor_tasks.txt`); `TASK_SOURCE_FILE=<path>` overrides it.
DeepSearchQA runs from Harbor Hub and requires an OpenAI-compatible judge endpoint; see
[Tasks/DeepSearchQA/README.md](../Tasks/DeepSearchQA/README.md).

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

Run from the repo root:

```bash
python3 -m unittest discover -s Agents/Openclaw/tests
bash Agents/Openclaw/tests/test_build_openclaw_image.sh
bash Agents/Openclaw/tests/test_session_layout.sh
bash Agents/Openclaw/tests/test_start_session_tui.sh
bash Agents/Openclaw/tests/test_stream_openclaw_session_sh.sh
```
