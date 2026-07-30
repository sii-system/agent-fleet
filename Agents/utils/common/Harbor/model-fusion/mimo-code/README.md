# MimoCode / Mimo Max Harbor Glue

This directory provides an isolated Agent Fleet entry point for Fusion
Router's `mimo_max` pipeline. It mirrors the isolation rule used by the
sibling `model-fusion` wrapper: normal Harbor runs do not import these files.

The Router checkout defaults to a sibling `sii-fusion-router` directory. Real
gateway and Opik credentials belong in the repository's ignored
`config.local.env`, never in this directory.
The host must provide `uv`, which builds the Router's dynamic-version wheel.

```bash
# Build the pinned Router wheel and validate pipeline selection.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh doctor

# Validate one task without starting a trial.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh dry-run fix-git

# Run one task once.
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh smoke fix-git

# Run a configured task list, for example 11 tasks with five trials each.
TASK_SOURCE_FILE="$PWD/Tasks/Terminal-bench-2/harbor_terminalbench21_mimocode_11_tasks.txt" \
N_ATTEMPTS=5 \
TB_RUNS=5 \
TOTAL_WORKERS=11 \
TB_N_CONCURRENT=11 \
bash Agents/utils/common/Harbor/model-fusion/mimo-code/run_tb21.sh full
```

Important overrides:

- `FUSION_ROUTER_DIR`: Router checkout.
- `MIMO_ROUTER_SOURCE_CONFIG`: source Router JSON config.
- `MIMO_ROUTER_DIST_DIR`: wheel and derived-config cache.
- `MODEL`: main Claude model and the target behind the `sonnet` aliases.
- `TB_AGENT_TIMEOUT_MULTIPLIER`: defaults to `20`.
- `DETACH`: defaults to `1`.

The derived config sets `routing.max_fusions=-1` and selects `sonnet` for the
Mimo sampler, selector, and verifier. Fleet maps that alias to `MODEL`.
Router artifacts and `router-run-summary.json` are written under each Harbor
trial's `/logs/agent/` directory.
