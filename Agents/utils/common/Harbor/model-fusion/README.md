# Mid-Turn Fusion Harbor Glue

This directory contains the single-task Harbor wrapper for Claude Code
mid-turn fusion. Prompt construction, templates, and barrier state-machine code
are owned by the sibling `sii-fusion-router` checkout. Fleet does not vendor
fallback copies or expose a CLI fusion path.

The wrapper resolves three host-side Router paths and one container target:

- builder: `$FUSION_ROUTER_DIR/src/sii_fusion_router/frontends/claude_code/task_subagent_prompt.py`
- mounted frontend directory: `$FUSION_ROUTER_DIR/src/sii_fusion_router/frontends/claude_code`
- canonical outer prompt: `$FUSION_ROUTER_DIR/prompts/mid_turn_fusion/outer.md`
- container gate: `/opt/tb-fusion-round/subagent_barrier_gate.py`

Run one Terminal-Bench task with:

```bash
TASK_ID=fix-git \
SPAN_FORCE_MODE=mid-turn-fusion \
bash Agents/utils/common/Harbor/model-fusion/run_one_tb21_task.sh
```

This wrapper has exactly one execution path: Router prepare, the Claude Code
in-session gate/panels/`span-outer`, and Router finalize. Set
`FUSION_ROUTER_DIR` when the sibling checkout is not at the default location.
A missing builder, gate, canonical outer prompt, or `templates/` directory is a
hard preflight error showing the absolute missing path.

For a host-only wiring check that stops after contract generation, set
`MID_TURN_PREPARE_ONLY=1`.

The original implementation provenance is the locked
`origin/task-fusion-router` source branch; only its mid-turn reachable wiring
was retained here.

The shared Harbor files contain thin source/call hooks only. In-session
defaults, agent environment arguments, mount extension helpers, the single-task
wrapper, documentation, and wiring tests live in this directory. The normal
Harbor worker loop is unchanged. `Agents/Harbor-claude-code/sitecustomize.py`
remains in place because its import location is the Harbor startup contract.
