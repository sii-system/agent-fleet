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
`$OUTPUT_PATH/summary.txt` (counts, reward rollup, result paths); registry
summaries also record completion status and Harbor's exit code. Successful
fixed benchmark sessions close by default, and a foreground `start.sh` prints
that summary and returns Harbor's exit code. Interactive or detached failed
registry runs keep the final pane open so diagnostics do not disappear behind
Zellij's exit message; press `Ctrl-q` after inspection. Noninteractive
foreground failures return immediately. Set
`HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0` to keep successful final panes open too.
All per-task results stay on disk under `$OUTPUT_PATH`.

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

For OpenCode fixed benchmark runs, optional generation controls can be set in
`config.local.env` or the shell:

```bash
HARBOR_TEMPERATURE=0.2
HARBOR_TOP_P=0.9
HARBOR_MAX_TOKENS=8192
```

`HARBOR_MAX_TOKENS` also applies to Claude Code. Claude Code does not expose
temperature or top-p controls, so the runner rejects those two settings when
`AGENT=claude-code` instead of silently ignoring them. Rollout mode keeps its
separate `RL_TEMPERATURE`, `RL_TOP_P`, and `RL_MAX_NEW_TOKENS` interface.

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

`start.sh` automatically starts one monitor for each Harbor benchmark run,
including detached zellij runs and command-mode runs such as
`bash start.sh ./harboropik.sh`. Set `HARBOR_MONITOR_ENABLED=0` to disable it.
The monitor reads Fleet queue artifacts for local datasets and Harbor job/trial
results for registry datasets.

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

## Harbor Analyzer

`start.sh` starts the Pi-backed analyzer under the same Harbor run lifecycle by
default when the monitor is enabled. Set `HARBOR_ANALYZER_ENABLED=0` to disable it:

```bash
HARBOR_ANALYZER_ENABLED=0 ./start.sh --detach
```

For a foreground run without zellij, pass the Harbor command to `start.sh`:

```bash
bash start.sh ./harboropik.sh
```

The analyzer depends on the monitor. It follows
`monitor/analyzer-handover-latest.json` and `monitor/analyzer-handoffs/`, writes
reports under `$OUTPUT_PATH/analyzer`, and does not restart, stop, or otherwise
control the benchmark run. Before using the default analyzer path, configure
`BASE_URL`, `API_KEY`, and `MODEL`, or set the analyzer-specific
`HARBOR_ANALYZER_BASE_URL`, `HARBOR_ANALYZER_API_KEY`, and
`HARBOR_ANALYZER_MODEL` overrides. If no analyzer model gateway should be used
for a run, set `HARBOR_ANALYZER_ENABLED=0`.

## Harbor Fixer: Plan Generation

`scripts/fixer.py` reads Analyzer output artifacts and generates a validated
`fix-plan-latest.json`. From `analyzer-artifacts-latest.json`, Fixer selects
each handover's current publication and reads:

```text
env-infra-tasks/<handover-id>/<publication-id>.json
fix-line-index/<handover-id>/<publication-id>.jsonl
```

`--analyzer-output` must point to the Analyzer root containing the manifest.
All unique environment and infrastructure failures across the benchmark's
handovers are planned together. If the same task identity appears more than
once, the later publication entry in the manifest supersedes the earlier
snapshot.

It builds one input per unique failure, asks isolated no-tool Pi agents for
task summaries, and asks one planning agent to group shared fixes. Before model
calls, the Python harness records a bounded
`target-environment.json` runtime inventory and redacted
`target-context.json` workspace/evidence snapshot. Both are embedded in
`main-agent-input.json`; the agents receive no filesystem tools.

Prepare inputs and prompts without invoking Pi:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --prepare-only \
  --write-prompts
```

Generate a plan:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --pi-bin pi \
  --pi-provider harbor-fixer \
  --pi-model "$HARBOR_FIXER_MODEL" \
  --pi-base-url "$BASE_URL" \
  --workspace-root /path/to/workspace \
  --max-concurrency 4 \
  --max-task-summary-chars 24000 \
  --max-task-summaries-chars 400000
```

The two summary limits are optional and default to the values shown. Oversized
summaries are omitted and recorded in `generation_errors`.

Each invocation uses an isolated `pi --mode json --print --no-session`
subprocess. Task summarizers use `thinking=off`; the plan agent retains the
configured thinking level. Events, stderr, prompts, and provenance are retained
under the output directory. No default model is assumed. An Analyzer snapshot
with no env/infra tasks produces an empty fix plan without invoking Pi.
Starting generation removes any stale `fix-plan-latest.json`; if no task
summary succeeds, Fixer writes a diagnostic empty plan and exits nonzero.

## More Details

Architecture, script roles, task resolution, and full variable descriptions are in [STRUCT.md](./STRUCT.md).
