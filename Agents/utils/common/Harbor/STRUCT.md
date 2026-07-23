# Harbor Common Structure

This directory contains shared Harbor orchestration code. It does not own task lists or agent-specific integration code.
RL rollout implementation lives in `Agents/utils/rl`; this directory only
keeps the shared entry point and Harbor runner wrappers.

## Files

```text
Agents/utils/common/Harbor/
├── start.sh                    # Main zellij launcher
├── env.sh                      # Path resolution and runtime defaults
├── gen_harbor_zellij_layout.sh # zellij layout generator
├── monitor_harbor.sh           # Run monitor pane
├── run_harbor_worker.sh        # Worker loop for one zellij pane
├── run_harbor_registry.sh      # Registry runner with optional final-pane hold
├── harboropik.sh               # Harbor CLI wrapper with Opik setup
├── prepare_local_deps.sh       # Local package/cache preparation
├── runner-requirements.txt     # Exact direct dependencies for the runner image
├── setup_runner_env.sh         # Explicit host setup / image validation
├── harbor_prepare_runner_cli.py # Startup validation for the configured CLI
├── harbor_worker_utils.py
└── scripts/
    ├── monitor.py              # Monitor CLI entrypoint and path resolution
    ├── analyzer_subagent.py    # Analyzer entrypoint for Pi/GLM-5.2 root-cause analysis
    ├── harbor_analyzer/        # Contract validation, fixed prompt, Pi dispatch, output validation
    ├── harbor_monitor/
    │   ├── artifacts.py        # Queue, result, manifest, environment, and state I/O
    │   ├── classification.py   # Task and benchmark status classification
    │   ├── evaluator.py        # Compose one monitor sample
    │   ├── contracts.py        # User, analyzer, and runner output contracts
    │   └── runner.py           # Control commands, retries, and follow loop
    ├── online_rule_analyzer.py # Optional console-only online analysis
    └── write_harbor_registry_summary.py # Native registry summary writer
```

Analyzer architecture and output boundaries are documented in
[ANALYZER_ARCHITECTURE.md](ANALYZER_ARCHITECTURE.md). Analyzer credentials stay
in the environment as `HARBOR_ANALYZER_*` variables.

```text
Agents/utils/rl/
├── RL-env.sh                         # RL rollout defaults
├── rollout_remote_harbor.py          # Miles/Polar-compatible HTTP listener
├── run_rl_rollout_server.sh          # Listener lifecycle wrapper
├── ensure_rl_job_zellij.sh           # Per-submission zellij launcher
├── gen_rl_rollout_zellij_layout.sh   # Per-submission zellij layout generator
├── monitor_rl_rollout.sh             # RL job monitor pane
├── run_rl_rollout_worker.sh          # Queue worker that reuses harboropik.sh
└── rl_dataset_worklist.py            # Dataset-to-task-list helper
```

## Path Resolution

`env.sh` computes paths relative to this file:

- `REPO_ROOT`: repository root
- `AGENTS_DIR`: `REPO_ROOT/Agents`
- `TASKS_DIR`: `REPO_ROOT/Tasks`
- `HARBOR_CLAUDE_CODE_DIR`: Claude Code integration directory
- `HARBOR_OPENCODE_DIR`: OpenCode integration directory

## Task Resolution

`DATASET_NAME` selects a built-in local dataset alias:

- `seta`: `Tasks/SETA/harbor_tasks.txt`
- `sweverify`: `Tasks/SWE-verify/harbor_tasks.txt`
- `smith`: `Tasks/SWE-smith/harbor_tasks.txt`
- `terminalbench21`: `Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt`

`TASK_SOURCE_FILE` overrides the built-in selection. If `DATASET_NAME` contains
`/` or `@`, it is treated as a Harbor registry dataset id and passed directly to
`opik harbor run --dataset`. Registry datasets can use the normal zellij
entrypoint as a single-pane wrapper around `harboropik.sh`; the local
multi-worker queue mode still expects a materialized `TASK_FILE`.

Typical dataset paths:

| Dataset | `DATASET_NAME` | `DATASET_PATH` | Metric | Suggested workers |
| --- | --- | --- | --- | --- |
| SETA | `seta` | `/workspace/seta-env/Harbor-Dataset` | success rate | `80` |
| SWE-Smith | `smith` | `/workspace/harbor/datasets/swesmith` | reward | `80` |
| Terminal-Bench 2.1 | `terminalbench21` | `/workspace/terminal-bench-2-1/tasks` | success rate | `20` |
| SWE-bench Verified | `sweverify` | `/workspace/swebench-verified` | success rate | `20` |
| Harbor registry dataset | `owner/name` or `owner/name@version` | unset | set `METRIC_MODE` if needed | runner concurrency |

## Main Variables

| Variable | Purpose |
| --- | --- |
| `AGENT` | `claude-code` or `opencode` |
| `MODEL` | Model name passed to Harbor |
| `BASE_URL` | Model gateway base URL |
| `API_KEY` | Model gateway API key |
| `DATASET_NAME` | Built-in local dataset selector, or Harbor registry dataset id |
| `DATASET_PATH` | Local dataset directory |
| `TASK_SOURCE_FILE` | Explicit task list path |
| `TOTAL_WORKERS` | Number of zellij workers |
| `TB_N_CONCURRENT` | Harbor concurrency, normally the same as `TOTAL_WORKERS` |
| `RUN_ID` | Run name |
| `OUTPUT_ROOT` | Parent directory for runs |
| `OUTPUT_PATH` | Full output directory |
| `HARBOR_ZELLIJ_CLOSE_ON_COMPLETE` | `1` closes fixed benchmark sessions after summary generation; `0` keeps the final pane open |
| `OPIK_URL` | Opik API URL, usually ending in `/api` |
| `OPIK_URL_OVERRIDE` | Opik API URL forwarded into task containers |
| `OPIK_API_KEY` | Opik API key |
| `OPIK_PROJECT_NAME` | Opik project name |
| `TRACE_PLUGIN_SOURCE_DIR` | Tracing source path, defaults to `third_party/agent-opik-plugin` |
| `TRACE_TO_OPIK` | Enables or disables trace upload |
| `CLAUDE_CODE_VERSION` | Claude Code package version used by local dependency cache |
| `OPENCODE_VERSION` | OpenCode package version used by local dependency cache |
| `LOCAL_WHEEL_DIR` | Local dependency cache directory |
| `LOCAL_WHEEL_PORT` | Preferred local dependency HTTP server port |
| `LOCAL_WHEEL_PORT_ATTEMPTS` | Number of local port attempts |
| `TB_REMOTE_WHEEL_SERVER_URLS` | Comma-separated fallback dependency cache URLs |
| `TB_SKIP_DOCKERHUB_PREFLIGHT` | Skip Docker Hub preflight connectivity check |
| `TB_FORCE_BUILD` | Build task images locally instead of using prebuilt images |
| `TB_TIMEOUT_MULTIPLIER` | General Harbor timeout multiplier |
| `TB_AGENT_TIMEOUT_MULTIPLIER` | Agent execution timeout override |
| `TB_AGENT_SETUP_TIMEOUT_MULTIPLIER` | Agent setup timeout multiplier |
| `HARBOR_ONLINE_ANALYSIS` | Enables console-only online analysis, default `0` |
| `HARBOR_EARLY_STOP` | Stops the current SETA task on matching task-blocking online-analysis events when set to `1`, default `0` |

## RL Rollout Variables

Set `ROLLOUT=1` to start the remote Harbor service instead of a fixed dataset
zellij run.  `env.sh` then sources `Agents/utils/rl/RL-env.sh` through
`RL_ENV_FILE`.

| Variable | Purpose |
| --- | --- |
| `RL_UTILS_DIR` | Directory containing rollout scripts, defaults to `Agents/utils/rl` |
| `RL_ENV_FILE` | Optional rollout config file, defaults to `$RL_UTILS_DIR/RL-env.sh` |
| `RL_HOST` / `RL_PORT` | Listener address for `/health`, `/datasets`, and `/run_trial` |
| `RL_DATASET_NAME` | Default dataset name exposed to RL callers |
| `RL_DATASET_ROOT` | Default dataset root used to resolve task ids |
| `RL_DATASET_ROOTS` | Comma-separated `name=path` aliases for additional datasets |
| `RL_AGENT` | Agent used by rollout workers, normally `claude-code` or `opencode` |
| `RL_MODEL_NAME` | Model name used when a request does not provide one |
| `RL_API_BASE` / `RL_API_KEY` | Model gateway defaults for rollout requests |
| `RL_MODEL_INFO` / `RL_MAX_NEW_TOKENS` | Harbor model/token budgets applied to rollout tasks |
| `RL_MAX_TURNS` | Mapped to Harbor `max_turns` for claude-code rollout tasks |
| `RL_FORCE_BUILD` | Mapped to `TB_FORCE_BUILD`; request `force_build` can override it |
| `RL_AGENT_TIMEOUT_MULTIPLIER` | Mapped to Harbor's agent timeout multiplier |
| `RL_LLM_TIMEOUT` / `RL_LLM_MAX_RETRIES` | Mapped into `llm_kwargs.timeout` and `llm_kwargs.max_retries` |
| `RL_TEMPERATURE` / `RL_TOP_P` / `RL_TOP_K` / `RL_MIN_P` | Mapped into rollout `llm_kwargs` sampling fields |
| `RL_COLLECT_ROLLOUT_DETAILS` / `RL_ENABLE_SUMMARIZE` | Mapped to claude-code agent kwargs in rollout mode |
| `RL_MAX_CONCURRENT` / `RL_WORKERS` | Worker count for each per-submission zellij session |
| `RL_QUEUE_DIR` | Shared queue root for rollout requests |
| `RL_JOB_QUEUE_ROOT` | Per-submission queue root |
| `RL_JOB_RUNTIME_ROOT` | Per-submission zellij runtime root |
| `RL_TRACE_LOG` | JSONL request/result event log with API keys removed |
| `RL_DYNAMIC_JOB_ZELLIJ` | Create one zellij session per Ray submission when enabled; rollout requests are rejected if this is disabled without another worker pool |

## Agent Variables

Claude Code specific defaults:

- `CLAUDE_CODE_VERSION`
- `TB_ANTHROPIC_BASE_URL`
- `TB_ANTHROPIC_AUTH_TOKEN`
- `TB_ANTHROPIC_MODEL`
- `TB_ANTHROPIC_DEFAULT_OPUS_MODEL`
- `TB_ANTHROPIC_DEFAULT_SONNET_MODEL`
- `TB_ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `TB_CLAUDE_CODE_SUBAGENT_MODEL`
- `TB_CLAUDE_CODE_EFFORT_LEVEL`
- `TB_CLAUDE_CODE_MAX_OUTPUT_TOKENS`
- `TB_CC_HOOK_SOURCE`

OpenCode specific defaults:

- `OPENCODE_VERSION`
- `OPENCODE_PROVIDER`
- `OPENCODE_CONFIG_CONTENT`
- `TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE`
- `TRACE_PLUGIN_OPENCODE_HOOK_SOURCE`

## Opik Plugin Submodule

The tracing plugin is linked as a Git submodule at
`third_party/agent-opik-plugin`, pinned to tag `v0.1.0`. Initialize it before
running:

```bash
git submodule update --init --recursive
```

The runner checks these files when tracing is enabled:

```text
Claude Code:
  third_party/agent-opik-plugin/src/sii_opik_plugin/claude_code/claude_realtime_trace.py

OpenCode:
  third_party/agent-opik-plugin/harness/opencode/opik-trace.ts
  third_party/agent-opik-plugin/src/sii_opik_plugin/opencode/opencode_realtime_trace.py
```

## Execution Flow

1. `start.sh` sources `env.sh`, validates `AGENT`, initializes output directories, and prepares the task file.
2. `gen_harbor_zellij_layout.sh` writes a zellij layout with monitor and worker panes.
3. Each worker pane runs `run_harbor_worker.sh`.
4. Workers claim tasks from the shared queue and call `harboropik.sh`.
5. `harboropik.sh` prepares Opik/tracing settings and invokes Harbor with either Claude Code or OpenCode.
6. When every task is done or failed, `monitor_harbor.sh` writes `$OUTPUT_PATH/summary.txt`, stops the online analyzer, and exits when `HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=1` (the default). With `0`, the final monitor pane remains open for inspection.

Native registry runs use the single-pane layout; `run_harbor_registry.sh`
wraps `harboropik.sh`, which writes the same summary path from Harbor's job
result. The wrapper keeps the final registry pane open when the completion
switch is `0`.

RL rollout flow:

1. `ROLLOUT=1 start.sh` skips fixed dataset preparation and starts
   `Agents/utils/rl/run_rl_rollout_server.sh`.
2. `rollout_remote_harbor.py` accepts `/run_trial`, requires a top-level
   `ray_submission_id`,
   resolves the requested dataset task, and writes one request JSON into the
   matching per-submission queue.
3. `ensure_rl_job_zellij.sh` creates a
   `harbor-rollout-<agent>-<dataset>-<ray_submission_id>` zellij session for
   that submission if one is not already running.
4. `run_rl_rollout_worker.sh` claims queued requests and calls
   `Agents/utils/common/Harbor/harboropik.sh`, preserving normal agent logs,
   local dependency cache behavior, Opik tracing, and timeout finalization.
5. The worker writes the result JSON; the HTTP request returns that result.

RL rollout sessions are not governed by `HARBOR_ZELLIJ_CLOSE_ON_COMPLETE`.
Their monitor and worker panes are long-running because requests are queued
dynamically, so rollout session retirement requires a separate idle policy.

## Optional Harbor Monitor Diagnostics

When `HARBOR_ONLINE_ANALYSIS=1`, `start.sh` launches a tailer. For most
datasets it reads:

```text
<OUTPUT_PATH>/<task-id>-<task-name>.console.log
```

For `DATASET_NAME=seta`, it reads Harbor job logs instead:

```text
<OUTPUT_PATH>/jobs/**/job.log
```

`harboropik.sh` emits `[ONLINE_ENV]` JSON lines after deterministic setup
checks fail. The analyzer marks allowlisted fatal events as task-blocking and
reports raw text matches. By default this is report-only. For SETA only,
`HARBOR_EARLY_STOP=1` makes each worker stop its current task when the
online-analysis events file contains a matching `task_blocking=true` event for
that task. `RESET_RUN=1` clears the online-analysis state before restarting the
tailer so old task-blocking events are not reused. Opik observability failures
are excluded because they do not determine task execution status. The existing
Harbor monitor displays aggregated structured environment signals next to its
exception statistics. Agent exceptions remain in the existing exception
statistics section to avoid duplicate reporting.

Outputs:

```text
<OUTPUT_PATH>/online-analysis/environment-events.jsonl
<OUTPUT_PATH>/online-analysis/environment-summary.json
```
