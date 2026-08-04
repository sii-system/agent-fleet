# OpenRouter Fusion Harbor Glue

This directory is the isolated Agent Fleet entry point for Fusion Router's
original `openrouter_fusion` pipeline. It is independent of the sibling
mid-turn and `mimo-code` integrations; normal Harbor runs do not import it.

The wrapper builds the selected Router checkout, derives a config whose panel,
reviewer, outer, and checklist aliases all resolve through Fleet's `MODEL`,
mounts the wheel/config read-only, and wraps only the enabled Claude command.
Credentials and Opik settings remain in the ignored `config.local.env`.

```bash
# Build and validate the original pipeline.
bash Agents/utils/common/Harbor/model-fusion/openrouter/run_tb21.sh doctor

# Validate mounts and Harbor arguments without starting a task.
bash Agents/utils/common/Harbor/model-fusion/openrouter/run_tb21.sh dry-run configure-git-webserver

# Run one task once, detached by default.
bash Agents/utils/common/Harbor/model-fusion/openrouter/run_tb21.sh smoke configure-git-webserver

# Run the 11-task list five times.
TASK_SOURCE_FILE="$PWD/Tasks/Terminal-bench-2/harbor_terminalbench21_mimocode_11_tasks.txt" \
N_ATTEMPTS=5 TB_RUNS=5 TOTAL_WORKERS=11 TB_N_CONCURRENT=11 \
bash Agents/utils/common/Harbor/model-fusion/openrouter/run_tb21.sh full
```

Important overrides:

- `FUSION_ROUTER_DIR`: Router checkout.
- `OPENROUTER_SOURCE_CONFIG`: source Router JSON.
- `OPENROUTER_DIST_DIR`: wheel and derived-config cache.
- `OPENROUTER_MAX_FUSIONS`: defaults to `-1`; set a nonnegative limit if needed.
- `MODEL`: gateway model used for every Router role.
- `TB_AGENT_TIMEOUT_MULTIPLIER`: defaults to `20`.
- `DETACH`: defaults to `1`.

Artifacts are written under each trial's `/logs/agent/router/`; the per-run
summary is `/logs/agent/router-run-summary.json`.
