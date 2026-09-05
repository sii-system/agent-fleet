# Harbor Runner

This directory contains the shared Harbor runner for Claude Code, OpenCode,
and Pi.

For YiCloud OpenSandbox, start with the
[OpenSandbox quick start](OPENSANDBOX_README.md). For qz (SII Inspire)
sandboxes, start with the [qz Sandbox quick start](QZ_SANDBOX_README.md).

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
# Set OPIK_URL to upload traces; leave it out to run without Opik:
OPIK_URL=http://your-opik-host/api
OPIK_PROJECT_NAME=your-project-name
```

Then edit the run parameters in `env.sh`:

```bash
AGENT="claude-code"        # claude-code, opencode, or pi
DATASET_NAME="seta"        # built-in Harbor registry alias
TOTAL_WORKERS="80"
HARBOR_N_CONCURRENT="80"
```

For OpenCode fixed benchmark runs, optional generation controls can be set in
`config.local.env` or the shell:

```bash
HARBOR_TEMPERATURE=0.2
HARBOR_TOP_P=0.9
HARBOR_MAX_TOKENS=8192
```

`HARBOR_MAX_TOKENS` also applies to Claude Code and Pi. Neither exposes
temperature or top-p controls, so the runner rejects those two settings for
`AGENT=claude-code` and `AGENT=pi` instead of silently ignoring them. The Pi
provider derives from `BASE_URL` when `PI_PROVIDER` is unset. Rollout mode
keeps its separate `RL_TEMPERATURE`, `RL_TOP_P`, and `RL_MAX_NEW_TOKENS`
interface. For Pi, `MODEL` is always treated as an opaque model ID; `/` remains
part of that ID and never selects a provider.

When `OPIK_URL` is set, the Opik tracing plugin is loaded from
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
results for registry datasets. Internally, `harbor_monitor` produces the
observation while `harbor_controller` selects the state-appropriate action and
retains run-local command execution behind a separate boundary. Restart and
stop are not executed automatically from observations.

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
inside `RUN_DIR`; arguments are allowed but shell syntax is not. The observation
policy never invokes them by itself. When the controller is awaiting a decision,
the user can explicitly choose an allowed action with the run-local decision
CLI:

```bash
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" status
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" decide wait --wait-seconds 300
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" decide stop
```

`restart` is accepted instead of `stop` after an abnormal exit when it appears
in `allowed_decisions`. The CLI atomically writes
`monitor/user-decision.json`; the already-running monitor validates the run,
incident, allowed action, and unique decision ID before using the existing
controller executor. `wait` defers the incident without external control.
`restart` and `stop` execute only their configured run-local command.

Optional run-local controls can be set with
`HARBOR_MONITOR_RESTART_CMD` and `HARBOR_MONITOR_STOP_CMD`.

| Output | Used by | Content |
| --- | --- | --- |
| `monitor-latest.json` | Debugging | Full state and evidence |
| `user-notify-latest.json` | User | Objective status and required human action |
| `user-decision.json` | Monitor/controller | Explicit decision submitted for the current notification |
| `analyzer-handover-latest.json` | Analyzer | Tasks requiring deeper analysis |
| `runner-action-latest.json` | Runner | `wait`, `restart`, `stop`, or `notify`, plus execution result |

All files are refreshed on each sample. The actual action is
`runner-action-latest.json.type`; the user report filename does not imply
`notify` was triggered.

| Observed state | Action |
| --- | --- |
| Worker active and making progress | `wait` |
| Worker active past `--configured-timeout` | `notify`; `wait` is allowed, and `stop` is allowed when a stop command is configured |
| Worker active after the confirmed stall duration | `notify`; `wait` is allowed, and `stop` is allowed when a stop command is configured |
| Tasks unfinished with no live worker | `notify`; `restart` is allowed below `--max-retries` when a restart command is configured |
| Every task has a terminal queue record | `wait`; finish the monitor loop without external control |

The interaction path is available whenever `HARBOR_MONITOR_ENABLED=1`; it has
no separate feature flag or daemon. The monitor reads the decision file only
while `controller_status=awaiting_user_decision`. Normal progressing and
completed samples do not read it. The CLI and controller do not call zellij or
Opik APIs, so their existing lifecycle and telemetry paths remain unchanged.
The monitor does not consume restart attempts or execute restart/stop commands
without a matching explicit user decision.

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

## Harbor Fixer

Harbor Fixer consumes Analyzer output, generates a Fix Plan, checks every
action against execution policy, and executes an allowed plan.

The Controller provides the minimal user-controlled workflow for a completed
or explicitly stopped benchmark. `fixer start` runs planning and policy only;
it does not modify the workspace. Review the returned `approval.plans` (also
available under `controller.py ... status`) before approving the exact plan:

```bash
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" fixer start --workspace-root /path/to/workspace
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" status
python3 Agents/utils/common/Harbor/scripts/controller.py \
  --run-dir "$RUN_DIR" fixer approve --request-id "$APPROVAL_REQUEST_ID"
```

Use `fixer cancel --workflow-id "$FIXER_WORKFLOW_ID"` to reject a plan awaiting
approval. A cancellation requested during planning or policy review takes
effect at the next stage boundary. Approval synchronously executes the exact
plan, runs smoke verification, writes `fix-report-latest.json` and
`fix-report-latest.md`, and updates the existing `benchmark-summary.md` Fixer
section. These automatic follow-up steps
do not require additional user decisions and cannot be safely cancelled after
execution starts. Approval is bound to the run, workflow, approval request,
and SHA-256 digest of the reviewed Fix Plan; a changed plan is blocked instead
of executed.

`RESET_RUN=1` also coordinates with this workflow: reset is refused while a
planning, policy, execution, verification, reporting, or cancellation command
is still running. If that Controller process exited unexpectedly, the next
`fixer start` can recover the stale workflow state only when no tracked Fixer
process remains.

The Controller uses the repository `start.sh` directly for the isolated smoke
rerun; no restart, stop, or verification command configuration is required.
`controller.py ... status` exposes `verifying` and `reporting` while they run,
then reports the verification outcome and both report paths. Workflow control
state is written below `$RUN_DIR/fixer` as `fixer-state.json`,
`fixer-control-request.json`, `fixer-approval-request.json`, and
`fixer-user-decision.json` alongside the existing Fixer artifacts.

### Stage 1: Planning Context and Plan Generation

Point `--analyzer-output` at an Analyzer output directory containing
`analyzer-artifacts-latest.json`.

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

Generated artifacts, including `fix-plan-latest.json`, prompts, events, and
provenance, are written below `--output-dir`. The summary-limit options are
optional and use the defaults shown above. Fixer CLI flags override the
corresponding `HARBOR_FIXER_*` defaults loaded by `env.sh`.
Analyzer and Fixer Agent retries use bounded exponential backoff, starting at
one second and capped at 30 seconds. Override these values with
`HARBOR_AGENT_RETRY_INITIAL_SECONDS` and `HARBOR_AGENT_RETRY_MAX_SECONDS`.

### Stage 2: Execution Policy

Policy runs automatically before execution. Repeat `--policy-write-root` for
each directory in which `file_edit` actions may write; no directory is writable
by default. Use `--policy-rules` to load optional user allow and deny rules.
The decision is written to `execution-policy-decision.json`.

### Stage 3: Execute a plan

Execute validated actions in plan and action order:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --exec-only \
  --fix-plan /path/to/fixer-output/fix-plan-latest.json \
  --workspace-root /path/to/workspace \
  --execution-timeout 300 \
  --summary-limit 4000 \
  --policy-write-root /path/to/approved/config-root \
  --policy-rules /path/to/policy-rules.json \
  --output-dir /path/to/fixer-output
```

Execution writes `exec-input.json`, `execution-policy-decision.json`,
`exec-result-latest.json`, and action logs below `--output-dir`. A policy denial
blocks the complete plan set. A failed action skips the remainder of its plan;
later plans continue. `--execution-timeout` applies to each command action.

### Verify an executed plan

Verification is code-only and samples at most two successfully executed tasks
per plan by default:

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --verify-only \
  --fix-plan /path/to/fixer-output/fix-plan-latest.json \
  --exec-result /path/to/fixer-output/exec-result-latest.json \
  --verification-run-dir /path/to/new-harbor-run \
  --output-dir /path/to/fixer-output
```

Use `--rerun-command` to launch the smoke run. The wrapper receives an ordered
`TASK_SOURCE_FILE` and `HARBOR_FIXER_SMOKE_SELECTION`; it must preserve their
line-to-task mapping. `--rerun-timeout` limits that command to 3600 seconds by
default and can also be set with `HARBOR_FIXER_RERUN_TIMEOUT`. Task identities
come directly from Fix Plan v2.
While the rerun is active, stdout and stderr are written to
`runtime/<agent>/verification-rerun.stdout.log` and
`runtime/<agent>/verification-rerun.stderr.log`; the verifier also emits a
progress message every 30 seconds.
Verification writes
`verification-smoke-selection.json`, `verification-smoke-tasks.txt`, and
`verification-result-latest.json`. If the Fix Plan was generated without a
local monitor, select `claude-code`, `opencode`, or `oracle` with `--agent`.

### Generate a report

```bash
python3 Agents/utils/common/Harbor/scripts/fixer.py \
  --report-only \
  --verification-result /path/to/fixer-output/verification-result-latest.json \
  --analyzer-output /path/to/analyzer-output \
  --output-dir /path/to/fixer-output \
  --pi-model "$HARBOR_FIXER_MODEL" \
  --pi-base-url "$BASE_URL" \
  --baseline-run-dir /path/to/old-harbor-run
```

Reporter keeps task, execution, and verification observations code-owned. A
no-tool Pi agent may generate only the bounded human-readable summary. The
Markdown view presents observed results and unavailable data before attributed
Analyzer findings and Fix Plan reasoning. Smoke-test outcomes remain scoped to
sampled tasks. The machine contract is written to `fix-report-latest.json`; the
deterministic, secret-redacted view is written to `fix-report-latest.md`.

After Controller-approved execution and verification, Controller replaces only
the existing `## Fixer Results` section in `benchmark-summary.md`; it does not
regenerate the Monitor or Analyzer summary.

## More Details

Architecture, script roles, task resolution, and full variable descriptions are in [STRUCT.md](./STRUCT.md).
