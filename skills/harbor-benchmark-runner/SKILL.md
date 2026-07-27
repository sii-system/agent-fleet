---
name: harbor-benchmark-runner
description: Use when configuring, launching, monitoring, or debugging Harbor benchmark runs for Claude Code or OpenCode in this repository.
---

# Harbor Benchmark Runner

## Overview

Use this skill for Harbor benchmark runs from `Agents/utils/common/Harbor/`.
The main path is `Agents/utils/common/Harbor/start.sh` -> `env.sh` ->
zellij layout -> worker panes -> `run_harbor_worker.sh` -> `harboropik.sh`.

Keep task ownership in `Tasks/`, agent integration ownership in
`Agents/Harbor-claude-code/` or `Agents/Harbor-opencode/`, and shared
orchestration ownership in `Agents/utils/common/Harbor/`.

## Workflow

1. Read the active run config before changing anything:
   `Agents/utils/common/Harbor/env.sh`, root `config.env`, optional
   `config.local.env`, and the relevant task list under `Tasks/`.
2. Confirm the Opik plugin submodule exists when tracing is enabled:
   `git submodule update --init --recursive`.
3. Choose the dataset path deliberately:
   `DATASET_NAME=seta|smith|terminalbench21|sweverify` maps to the built-in
   `Tasks/` lists, while `TASK_SOURCE_FILE=<path>` overrides them.
4. Validate that `DATASET_PATH` contains the selected task IDs before launch.
   A Harbor error like `No tasks matched ... There are 0 tasks available` is a
   dataset/task-resolution failure, not an agent or model failure.
5. Keep `TOTAL_WORKERS` and `TB_N_CONCURRENT` aligned unless there is a
   specific reason to decouple zellij panes from Harbor concurrency.
6. Launch from `Agents/utils/common/Harbor/` with `bash start.sh --detach` for
   detached operation or `bash start.sh` for an interactive zellij session.
7. For Harbor registry datasets such as `owner/name` or `owner/name@version`,
   use the non-interactive entrypoint shown by `start.sh`; zellij worker mode
   requires a materialized task file.
8. If `HARBOR_ONLINE_ANALYSIS=1`, inspect
   `<OUTPUT_PATH>/online-analysis/environment-events.jsonl` and
   `environment-summary.json` before attributing failures to the agent.

## Configuration Rules

- Put real `API_KEY`, `OPIK_API_KEY`, gateway credentials, and private endpoint
  overrides only in `config.local.env` or the caller environment.
- Treat `config.env` as the public-safe template.
- Do not duplicate Harbor task lists under `Agents/`; built-in task lists live
  under `Tasks/`.
- Use `RESET_RUN=1` only when intentionally clearing run state for the selected
  `RUN_ID` and zellij session.
- `OUTPUT_PATH` defaults under `OUTPUT_ROOT` and contains console logs, task
  state, and optional online-analysis artifacts.

## E2E Run Notes

- For Claude Code in restricted-network environments, prefer a mounted
  `claude-code-<version>.tgz` plus npm cache over downloading from the public
  Claude installer during each task.
- Harbor mounts the Claude Code tgz and wheel/cache independently of the Opik
  hook. Point `TB_CC_CLAUDE_TGZ_SOURCE` at the local tgz and
  `TB_CC_PY_WHEEL_DIR_SOURCE` at the cache directory that contains
  `npm-cache/`; enable `TB_CC_OPIK_ENABLE_HOOK=1` only for traced runs.
- After a mounted-tgz run starts, confirm task `config.json`, `lock.json`, or
  `trial.log` includes `/opt/tb-opik/claude-code.tgz` and
  `/opt/tb-opik/python-wheels/npm-cache`. If the task still attempts
  `downloads.claude.ai`, report that as an install-rewrite/setup issue rather
  than an agent failure.
- Known working local dataset paths from E2E verification:
  - SETA numeric tasks: `/workspace/seta-env-camel/Harbor-Dataset`
  - TerminalBench21 verified tasks: `/workspace/terminal-bench-2-verified`
- SWE-smith and SWE-verify need extra dataset validation before judging agent
  quality. Ensure the dataset is materialized and that the selected task names
  appear in Harbor's available task list; otherwise failures before agent setup
  are operator/setup failures.

## Debugging

- Validate `AGENT` first: only `claude-code` and `opencode` are supported.
- For task selection issues, read `Agents/utils/common/Harbor/STRUCT.md` and
  compare `DATASET_NAME`, `DATASET_PATH`, and `TASK_SOURCE_FILE`.
- For worker failures, inspect the worker console log and the corresponding
  Harbor job log before changing scripts.
- For dependency-cache problems, inspect `prepare_local_deps.sh`,
  `LOCAL_WHEEL_DIR`, `LOCAL_WHEEL_PORT`, and
  `TB_REMOTE_WHEEL_SERVER_URLS`.
- For mounted Claude Code tgz problems, inspect `TB_CC_CLAUDE_TGZ_SOURCE`,
  `TB_CC_PY_WHEEL_DIR_SOURCE`, `TB_CC_CLAUDE_TGZ_MOUNT_PATH`, and
  `TB_CC_NPM_CACHE_MOUNT_PATH`, then grep task logs for public installer URLs.
- For tracing problems, verify `third_party/agent-opik-plugin` and the agent
  integration file listed in `Agents/utils/common/Harbor/STRUCT.md`.

## Output Contract

When reporting a Harbor run or fix, include:

- selected `AGENT`, `DATASET_NAME`, `DATASET_PATH` or `TASK_SOURCE_FILE`
- `RUN_ID`, `OUTPUT_PATH`, `TOTAL_WORKERS`, and `TB_N_CONCURRENT`
- exact launch command and whether it was detached
- strongest failure signal or current run state
- whether Claude Code was installed from mounted tgz/cache or from network
- files changed, if any
- validation command, usually an affected Harbor/OpenClaw test or a dry run of
  the edited script path

## Validation

Run only the affected checks unless the change spans subsystems:

```bash
python3 -m unittest discover -s Agents/utils/common/Harbor/tests
bash -n Agents/utils/common/Harbor/env.sh \
  Agents/utils/common/Harbor/start.sh \
  Agents/utils/common/Harbor/run_harbor_worker.sh \
  Agents/utils/common/Harbor/harboropik.sh \
  Agents/utils/common/Harbor/prepare_local_deps.sh
```

For Claude Code integration changes, also run:

```bash
python3 -m py_compile Agents/Harbor-claude-code/sitecustomize.py
```
