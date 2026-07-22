# Current Mid-Turn Fusion Integration

Fleet is an explicit Harbor integration layer. It locates a sibling Router
checkout, asks the Router builder to generate `claude-agents.json`, the system
prompt, and `fusion.json`, mounts the complete Router Claude frontend directory
read-only, and registers its PreToolUse and Stop gate alongside the existing
Opik hooks.

The execution contract is fixed to `mid-turn-fusion`:

1. A productive main-agent tool reaches the mounted gate.
2. The gate establishes `SPAN_MID_TURN_BOUNDARY_PANEL_REQUIRED` and supplies
   isolated panel workdirs under `SPAN_MID_TURN_ARTIFACT_ROOT`.
3. Claude Code runs `span-panel-0..N`, then `span-outer`.
4. `span-outer` returns `MID_TURN_MERGE_RESULT`.
5. The main actor applies that result, or continues fail-open without mutating
   the workspace when a clean apply is unavailable.
6. Router finalization records `mode=mid_turn_fusion` in `fusion.json`.

Router exposes only the in-session execution for mid-turn fusion. It registers
canonical `panel.md` and `outer.md` contracts and accepts one
`MID_TURN_OUTER_CONTEXT` schema. The workflow has one merge stage and is
strictly boundary -> panels -> span-outer -> apply/fail-open -> finalize.

There is no Fleet-owned prompt builder, barrier gate, panel runner, outer
runner, or CLI fusion adapter. This model-fusion wrapper has no alternate
backend. If the Router checkout is missing, fix `FUSION_ROUTER_DIR`; the wrapper
never falls back to a local core copy.

Fleet's shared Harbor scripts are deliberately thin: `env.sh` sources the
in-session defaults and `harboropik.sh` delegates the agent environment
arguments and read-only mounts. `run_harbor_worker.sh` remains the original
single-path worker loop.
