---
name: harbor-benchmark-runner
description: Use when configuring, launching, monitoring, or debugging Harbor benchmark runs for Claude Code, OpenCode, or Pi in this repository.
---

# Harbor Benchmark Runner

## Overview

Use this skill for fixed Harbor benchmark runs. The unified launcher is
`scripts/run_fleet.sh`; direct runs enter through
`Agents/utils/common/Harbor/start.sh` and `Agents/utils/common/Harbor/env.sh`.
Local datasets use worker queues; registry datasets use `run_harbor_registry.sh`
through the same zellij launcher. Both use `harboropik.sh`.

Task inputs belong in `Tasks/`, agent integrations in `Agents/Harbor-*/`, and
shared orchestration in `Agents/utils/common/Harbor/`. For a remote rollout
request, follow the rollout section of the
[Harbor README](../../Agents/utils/common/Harbor/README.md#rl-rollout-mode):
`ROLLOUT=1` starts the service under `Agents/utils/rl/`, not a fixed benchmark.

## Workflow

1. Read [Agents/AGENTS.md](../../Agents/AGENTS.md), the active runner defaults,
   and the requested configuration. Preserve precedence: caller environment
   (including explicitly empty values), `config.local.env`, then `config.env`.
   Inspect private configuration without printing credentials.
2. Check the configured host runner. `scripts/setup.sh` prepares the pinned
   environment; benchmark startup only validates it. Use explicit setup to
   address missing tools or dependency mismatches, preserving existing config.
3. Choose registry or local task resolution:
   - `seta`, `terminalbench21`, and `sweverify` resolve to registry datasets;
     full IDs such as `owner/name` or `owner/name@version` also work. They do
     not require a local `DATASET_PATH` or materialized worker task file.
   - `smith` stays local. For another local checkout, use an explicit path with
     `--taskset`, or `DATASET_NAME=auto` plus `DATASET_PATH` directly.
     `TASK_SOURCE_FILE` overrides the local list. Validate selected task IDs
     against that dataset before launching.
4. Preserve the requested agent and backend. The unified launcher accepts
   `claude-code`, `opencode`, and `pi`; direct Harbor also accepts `oracle`
   for reference solutions. `RL_ENVIRONMENT_TYPE` selects the backend, and
   fixed runs derive `HARBOR_ENVIRONMENT_TYPE` unless explicitly overridden.
   Read the relevant backend runbook below before preparing remote resources.
5. Bound a smoke run by task selection and attempt count. Worker count controls
   concurrency, not total tasks. `--task` selects exact names for built-in
   aliases and local paths; it is unsupported for arbitrary registry IDs and
   rollout mode. For a registry run, `HARBOR_LIMIT` bounds tasks; local runs
   require a selected task list. Set `N_ATTEMPTS=1` and `HARBOR_RUNS=1` when
   only one attempt is intended. Keep worker and concurrency counts aligned.
6. Launch with `scripts/run_fleet.sh --taskset ... --agent ... --workers ...`
   or `bash Agents/utils/common/Harbor/start.sh`. Both local and registry runs
   support `--detach`. Use `--dry-run` on the unified launcher to preview routing;
   it does not validate provider health or execute the benchmark. FleetSpec,
   prompt mode, and DinD usage are in [scripts/README.md](../../scripts/README.md).
7. Record the run receipt and inspect results under `OUTPUT_PATH` (default
   `<repo>/runs/<RUN_ID>`). Successful fixed sessions close by default and
   foreground runs print `summary.txt`; detached or interactive failed runs
   retain the final pane for inspection.

Example one-task canary from the repository root:

```bash
N_ATTEMPTS=1 HARBOR_RUNS=1 ./scripts/run_fleet.sh \
  --taskset terminalbench21 --task fix-git --agent pi --workers 1
```

## Configuration and Runtime

- `OPIK_URL` is the only tracing switch; empty disables upload, including when
  overriding a saved endpoint. `OPIK_API_KEY` is optional for authless Opik.
  Initialize `third_party/agent-opik-plugin` for traced runs with
  `git submodule update --init --recursive`. The Claude hook default follows
  the URL; it is not a separate tracing opt-in.
- `config.env` is public-safe. Real credentials and private endpoints belong in
  `config.local.env`, the caller environment, or the backend's ignored credential
  files. Do not include them in commands reported back to the user.
- Host dependency pins live in `runner-requirements.txt`. For task-container
  caches, inspect `prepare_local_deps.py`, `LOCAL_WHEEL_DIR`, and
  `HARBOR_REMOTE_WHEEL_SERVER_URLS`. For mounted Claude packages, inspect
  `HARBOR_CC_CLAUDE_TGZ_SOURCE`, `HARBOR_CC_PY_WHEEL_DIR_SOURCE`, and the
  configured mount paths. Check trial logs before diagnosing an installer fault.
- Pi uses a prepared runtime archive. `PI_THINKING_LEVEL` configures reasoning;
  local TypeScript extensions use `PI_EXTENSION_SOURCE` and require Docker or
  OpenSandbox. See [Harbor Pi](../../Agents/Harbor-pi/README.md).
- `HARBOR_TEMPERATURE` and `HARBOR_TOP_P` require OpenCode;
  `HARBOR_MAX_TOKENS` also applies to Claude Code and Pi. Rollout sampling has
  its own `RL_*` settings.
- Use a fresh `RUN_ID` for an independent run. `RESET_RUN=1` clears existing
  run state and is refused while an active Fixer workflow prevents reset.

Backend-specific setup and limitations:

- [E2B](../../Agents/utils/common/Harbor/E2B_README.md)
- [qz](../../Agents/utils/common/Harbor/QZ_SANDBOX_README.md)
- [OpenSandbox](../../Agents/utils/common/Harbor/OPENSANDBOX_README.md) and
  [Bundle/image preparation](../../Agents/utils/common/Harbor/OPENSANDBOX_IMAGE_MANAGER.md)

## Monitoring and Debugging

- Monitor and Pi-backed analyzer start by default for fixed runs. The analyzer
  uses the model gateway or `HARBOR_ANALYZER_*` overrides; set
  `HARBOR_ANALYZER_ENABLED=0` when analysis is not wanted. Inspect `summary.txt`,
  `monitor/monitor-latest.json`, `monitor/user-notify-latest.json`, and
  `analyzer/` alongside worker/trial logs. `HARBOR_MONITOR_ENABLED=0` disables
  the monitor and its dependent analyzer.
- Monitor observations do not automatically stop or restart runs. Use the
  run-local `scripts/controller.py` decision workflow when requested. Fixer
  planning precedes approval of the exact plan, execution, and smoke verification;
  analyzer findings alone do not authorize repairs. Commands and artifact
  contracts are in the [Harbor README](../../Agents/utils/common/Harbor/README.md).
- `HARBOR_ONLINE_ANALYSIS=1` enables separate console diagnostics under
  `online-analysis/`; these are distinct from the Pi-backed analyzer.
- Treat missing tasks, image preparation, runner validation, and dependency
  delivery failures as setup failures. Inspect the worker console and Harbor
  job/trial logs before attributing a failure to the model or verifier.

## Output Contract

Report the selected agent, backend, dataset ID or local path, exact task
selection and attempts, worker count, sanitized launch command, detached state,
`RUN_ID`, and `OUTPUT_PATH`. Include completion status, rewards, result paths,
and the strongest failure evidence. Describe cache/runtime delivery when relevant
to a failure, and list any files changed and checks run. A successful launcher
or dry run is not evidence that benchmark tasks passed.

## Validation

For edits, run affected suites with the Python/dependencies used by portable
CI, from the repository root:

```bash
PYTHONPATH=. python3 -m unittest discover -s Agents/utils/common/Harbor/tests
```

Also run the corresponding `Agents/Harbor-claude-code/tests`,
`Agents/Harbor-opencode/tests`, or `Agents/Harbor-pi/tests` suite for integration
changes, `scripts/tests` for launcher changes, and `Agents/utils/rl/tests` for
rollout changes. Run relevant shell regressions listed in
[Agents/AGENTS.md](../../Agents/AGENTS.md#development). Check each changed shell
file individually with `bash -n "$script"`; one `bash -n` invocation with several
filenames only parses the first file.
