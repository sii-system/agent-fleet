# MimoCode / Mimo Max Harbor Glue

This directory provides an isolated Agent Fleet entry point for Fusion
Router's `mimo_max` pipeline. It mirrors the isolation rule used by the
sibling `model-fusion` wrapper: normal Harbor runs do not import these files.

The Router checkout defaults to a sibling `sii-fusion-router` directory. Real
gateway and Opik credentials belong in the repository's ignored
`config.local.env`, never in this directory.
The host must provide `uv`, which builds the Router's dynamic-version wheel.
The wheel cache is keyed by the selected checkout's tracked and non-ignored
source content, including dirty and untracked files. Derived configs are
content-addressed and immutable, so overlapping runs cannot overwrite them.

```bash
# Build the pinned Router wheel and validate pipeline selection.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh doctor

# Validate one task without starting a trial.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh dry-run fix-git

# Run one task once.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh smoke fix-git

# Run the complete Terminal-Bench 2.1 list with five trials per task.
N_ATTEMPTS=5 \
TB_RUNS=5 \
TOTAL_WORKERS=20 \
TB_N_CONCURRENT=20 \
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh full
```

Important overrides:

- `FUSION_ROUTER_DIR`: Router checkout.
- `MIMO_ROUTER_SOURCE_CONFIG`: source Router JSON config.
- `MIMO_ROUTER_DIST_DIR`: wheel and derived-config cache root; relative paths
  are canonicalized before Harbor changes directories.
- `TASK_SOURCE_FILE`: optional task-list override; `full` defaults to
  `Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt`.
- `MODEL`: main Claude model and the target behind the `sonnet` aliases.
- `TB_AGENT_TIMEOUT_MULTIPLIER`: defaults to `20`.
- `DETACH`: defaults to `1`.

The derived config sets `routing.max_fusions=-1` and selects `sonnet` for the
Mimo sampler, selector, and verifier. Fleet maps that alias to `MODEL`.
Router artifacts and `router-run-summary.json` are written under each Harbor
trial's `/logs/agent/` directory.
