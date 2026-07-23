# Harbor Runner

This directory contains the shared Harbor runner for Claude Code and OpenCode.

The normal workflow is:

```bash
cd Agents/utils/common/Harbor
vim env.sh
bash start.sh --detach
```

Use `bash start.sh` instead of `--detach` for an interactive zellij session.

When every task has finished, the monitor writes a final summary to
`$OUTPUT_PATH/summary.txt` (counts, reward rollup, result paths). Fixed
benchmark sessions close by default; set `HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0`
to keep the final pane open for inspection. All per-task results stay on disk
under `$OUTPUT_PATH`.

This completion switch does not apply to RL rollout sessions, whose workers
serve a dynamic request queue rather than a fixed task total.

Optional console-only online analysis:

```bash
HARBOR_ONLINE_ANALYSIS=1 bash start.sh --detach
```

## Minimal Setup

Point the runner at your infrastructure. `config.env` is a committed template;
copy it to a git-ignored `config.local.env` (sourced after, and overriding,
`config.env`) and set your values — including credentials — there:

```bash
cp config.env config.local.env
vim config.local.env
```

Set your model gateway and tracing preference there. Opik endpoint values are
required only when tracing is enabled:

```bash
BASE_URL=https://your-openai-compatible-endpoint
API_KEY=your-api-key
MODEL=your-model-id
TRACE_TO_OPIK=false
# When TRACE_TO_OPIK=true:
OPIK_URL=http://your-opik-host/api
OPIK_PROJECT_NAME=your-project-name
```

Then edit the run parameters in `env.sh`:

```bash
AGENT="claude-code"        # claude-code or opencode
DATASET_NAME="seta"        # built-in Harbor registry alias
TOTAL_WORKERS="80"
TB_N_CONCURRENT="80"
```

When `TRACE_TO_OPIK=true` (the default), the Opik tracing plugin is loaded from
the `third_party/agent-opik-plugin` submodule. Initialize it before a traced run:

```bash
git submodule update --init --recursive
```

For a direct host run, first execute `./scripts/setup.sh` from the repository
root. It creates a pinned Harbor/Opik control environment under
`~/.local/share/agent-fleet/harbor-runner`. The DinD runner uses the
image-owned `/opt/harbor-runner` environment instead. Workload startup only
validates the selected environment and never installs or repairs it.

## Docker Compose Overlay

Harbor runs task containers through Docker. For DinD runners where the outer
container is privileged, this runner passes a default compose overlay that keeps
task containers unprivileged:

```yaml
# Agents/utils/common/Harbor/overlays/unprivileged-task.yaml
services:
  main:
    privileged: false
```

## Datasets

Use these values in `env.sh`:

| Dataset | `DATASET_NAME` | Typical `DATASET_PATH` | Suggested workers |
| --- | --- | --- | --- |
| SETA | `seta` | `/workspace/seta-env/Harbor-Dataset` | `80` |
| SWE-Smith | `smith` | `/workspace/harbor/datasets/swesmith` | `80` |
| Terminal-Bench 2.1 | `terminalbench21` | `/workspace/terminal-bench-2-1/tasks` | `20` |
| SWE-bench Verified | `sweverify` | `/workspace/swebench-verified` | `20` |

`seta`, `terminalbench21`, and `sweverify` download from the Harbor registry
by default. `smith` remains local. For an offline or local checkout of any
dataset, use `auto` with its path:

```bash
DATASET_NAME=auto \
DATASET_PATH=/workspace/seta-env/Harbor-Dataset \
bash Agents/utils/common/Harbor/start.sh --detach
```

For any Harbor registry dataset, pass the dataset id directly and use the normal
zellij entrypoint:

```bash
DATASET_NAME=openthoughts/tasktrove-swe-rebench-v2-patched-oracle \
bash Agents/utils/common/Harbor/start.sh --detach
```

Registry runs pass `--dataset "$DATASET_NAME"` to Harbor instead of preparing a
local task file from `DATASET_PATH`.

## RL Rollout Mode

Rollout mode exposes a Polar-compatible remote Harbor service instead of
starting a fixed dataset run.  It is gated by `ROLLOUT=1`; normal benchmark
runs are unchanged.

```bash
cd Agents/utils/common/Harbor
vim ../../rl/RL-env.sh
ROLLOUT=1 bash start.sh --detach
```

The service provides `GET /health`, `GET /datasets`,
`GET /datasets/{dataset_name}/tasks`, and `POST /run_trial`.  Requests are
queued, then per-submission zellij workers run the same `harboropik.sh` path as
normal benchmark workers, so task panes keep the regular agent/tool logs.

Each `/run_trial` request must include a top-level `ray_submission_id`. The
service uses it to create/reuse one
`harbor-rollout-<agent>-<dataset>-<ray_submission_id>` zellij session; requests
without it are rejected instead of being queued without workers.

For Docker usage, publish the listener port and run the same command inside the
container. Build the runner directly from this repository first:

```bash
docker build \
  -f scripts/dind/Dockerfile \
  -t agent-fleet-harbor-runner:local \
  .

docker run -d --name harbor-rollout \
  -p 19001:19001 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /workspace:/workspace \
  agent-fleet-harbor-runner:local sleep infinity

docker exec harbor-rollout bash -lc '
  cd /workspace/agent-fleet/Agents/utils/common/Harbor
  ROLLOUT=1 RL_HOST=0.0.0.0 RL_PORT=19001 bash start.sh --detach
'
```

For foreground debugging, run the same launcher without `--detach`. The
listener still creates per-submission zellij worker sessions for requests with
a top-level `ray_submission_id`.

```bash
cd Agents/utils/common/Harbor
ROLLOUT=1 bash start.sh
```

## Harbor Monitor

`start.sh` automatically starts one monitor for each Harbor benchmark run.
Set `HARBOR_MONITOR_ENABLED=0` to disable it. The monitor reads Fleet queue
artifacts for local datasets and Harbor job/trial results for registry datasets.

Equivalent queue monitor command:

```bash
RUN_DIR="$PWD/runs/example"
MONITOR_DIR="$RUN_DIR/monitor"

python3 Agents/utils/common/Harbor/scripts/monitor.py \
  --run-dir "$RUN_DIR" \
  --agent claude-code \
  --output "$MONITOR_DIR/monitor-latest.json" \
  --user-report-output "$MONITOR_DIR/user-notify-latest.json" \
  --analyzer-handover-output "$MONITOR_DIR/analyzer-handover-latest.json" \
  --runner-action-output "$MONITOR_DIR/runner-action-latest.json" \
  --follow --interval 30
```

Omit `--follow` for one sample. Control commands are optional executable files
inside `RUN_DIR`; arguments are allowed but shell syntax is not. If absent or
failed, the action becomes `notify`.

For automatic runs, optional run-local controls can be set with
`HARBOR_MONITOR_RESTART_CMD` and `HARBOR_MONITOR_STOP_CMD`.

| Output | Used by | Content |
| --- | --- | --- |
| `monitor-latest.json` | Debugging | Full state and evidence |
| `user-notify-latest.json` | User | Objective status and required human action |
| `analyzer-handover-latest.json` | Analyzer | Tasks requiring deeper analysis |
| `runner-action-latest.json` | Runner | `wait`, `restart`, `stop`, or `notify`, plus execution result |

All files are refreshed on each sample. The actual action is
`runner-action-latest.json.type`; the user report filename does not imply
`notify` was triggered.

| Observed state | Action |
| --- | --- |
| Worker active, including `suspected_stalled` | `wait` |
| Worker active past `--configured-timeout` | `notify` and continue monitoring |
| Tasks unfinished with no live worker | `restart`; after `--max-retries`, `notify` |
| Every task has a terminal queue record | `stop` |

Automatic restart is only used when tasks remain and no worker is alive.

## More Details

Architecture, script roles, task resolution, and full variable descriptions are in [STRUCT.md](./STRUCT.md).
