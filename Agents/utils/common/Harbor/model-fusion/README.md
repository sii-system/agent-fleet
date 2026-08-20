# Mid-Turn Fusion Harbor Glue

This directory contains the single-task Harbor wrapper for Claude Code
mid-turn fusion. Prompt construction, templates, and barrier state-machine code
are owned by the sibling `sii-fusion-router` checkout. Fleet does not vendor
fallback copies or expose a CLI fusion path.

The wrapper resolves four host-side Router paths and one container target:

- builder: `$FUSION_ROUTER_DIR/src/sii_fusion_router/frontends/claude_code/task_subagent_prompt.py`
- mounted frontend directory: `$FUSION_ROUTER_DIR/src/sii_fusion_router/frontends/claude_code`
- canonical panel prompt: `$FUSION_ROUTER_DIR/prompts/mid_turn_fusion/panel.md`
- canonical outer prompt: `$FUSION_ROUTER_DIR/prompts/mid_turn_fusion/outer.md`
- container gate: `/opt/tb-fusion-round/subagent_barrier_gate.py`

Run one Terminal-Bench task with:

```bash
TASK_ID=fix-git \
SPAN_FORCE_MODE=mid-turn-fusion \
bash Agents/utils/common/Harbor/model-fusion/run_one_tb21_task.sh
```

The wrapper defaults to `DATASET_NAME=auto` because both the task contract and
the Harbor run use `DATASET_PATH`. Its generated `RUN_ID` includes the task,
second-level timestamp, and process ID. If a caller deliberately reuses a
`RUN_ID` or `OUTPUT_PATH`, the wrapper regenerates the single-task list and
recreates only that task's model-fusion jobs directory before running.
`MAIN_MODEL` also replaces the shared Harbor-derived Anthropic model aliases;
aliases explicitly supplied by the caller remain unchanged.

This wrapper has exactly one execution path: Router prepare, the Claude Code
in-session gate/panels/`span-outer`, and Router finalize. Set
`FUSION_ROUTER_DIR` when the sibling checkout is not at the default location.
A missing builder, gate, canonical agent prompt, or `templates/` directory is
a hard preflight error showing the absolute missing path.

For a host-only wiring check that stops after contract generation, set
`MID_TURN_PREPARE_ONLY=1`.

The original implementation provenance is the locked
`origin/task-fusion-router` source branch; only its mid-turn reachable wiring
was retained here. The current transport baseline includes its recoverable
hook-message contract (`cabf5ab`) and file-backed Claude prompt transport
(`3ad61d4`).

The integration is isolated from normal Harbor runs. The shared `env.sh`,
`harboropik.sh`, worker loop, Claude `sitecustomize.py`, and skills are
unchanged. `run_one_tb21_task.sh` explicitly selects two local wrappers:

- `harboropik.sh` proxies only this run's Opik CLI call, adding the fusion agent
  environment and read-only Router/task mounts.
- `sitecustomize.py` loads the normal Claude/Opik patch, then layers on the
  fusion gate, `--agents`, and private file-backed prompt transport.

Consequently, none of this directory is sourced or imported unless the
single-task wrapper is invoked. Router owns the matching hook-message file:
each fusion writes `boundary.json`, and hook stdout returns the `BOUNDARY_FILE`
pointer instead of the full boundary payload.

The `mimo-code/` subdirectory contains the separately isolated `mimo_max`
Router CLI integration. It lives here so Fusion Router integrations have one
Harbor home, but it does not reuse or alter this mid-turn execution path.

The `openrouter/` subdirectory contains the original `openrouter_fusion`
pipeline using the same isolation rule. It is selected only through its own
`run_tb21.sh` entry point and does not modify Mimo Max or mid-turn runs.

The Mimo and OpenRouter launchers share `router_cli_utils.py` for Router source
fingerprinting, wheel build/metadata/extraction, derived config publication,
doctor validation, and task-list conversion. Their shell entry points still
own environment selection and Harbor process orchestration.
