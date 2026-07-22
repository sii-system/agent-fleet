#!/usr/bin/env bash
set -euo pipefail

model_fusion_build_agent_env_args() {
  MODEL_FUSION_AGENT_ENV_ARGS=(
    --ae "TB_CLAUDE_CODE_AGENTS_JSON=$TB_CLAUDE_CODE_AGENTS_JSON"
    --ae "TB_FUSION_ROUND_GATE=$TB_FUSION_ROUND_GATE"
    --ae "TB_FUSION_ROUND_GATE_PATH=$TB_FUSION_ROUND_GATE_PATH"
    --ae "TB_FUSION_ROUND_GATE_MODE=$TB_FUSION_ROUND_GATE_MODE"
    --ae "TB_FUSION_TASK_FILE=$TB_FUSION_TASK_FILE"
    --ae "FUSION_TASK_FILE=$TB_FUSION_TASK_FILE"
    --ae "SPAN_FORCE_MODE=$SPAN_FORCE_MODE"
    --ae "SPAN_FORCE_FUSION=$SPAN_FORCE_FUSION"
    --ae "SPAN_GATE_STATE_PATH=$SPAN_GATE_STATE_PATH"
    --ae "SPAN_MID_TURN_ARTIFACT_ROOT=$SPAN_MID_TURN_ARTIFACT_ROOT"
    --ae "SPAN_PANEL_MODELS=$SPAN_PANEL_MODELS"
    --ae "SPAN_PANEL_COUNT=${SPAN_PANEL_COUNT:-}"
    --ae "SPAN_MID_TURN_MAX_FUSIONS_PER_TASK=$TB_FUSION_MAX_FUSIONS_PER_TASK"
    --ae "SPAN_MID_TURN_PANEL_CALL_BUDGET=$TB_FUSION_PANEL_CALL_BUDGET"
  )
}

model_fusion_append_readonly_mounts() {
  local mounts_json="$1"

  if [[ "$TB_FUSION_ROUND_GATE" != "1" ]]; then
    printf '%s\n' "$mounts_json"
    return 0
  fi
  if [[ ! -d "$TB_FUSION_ROUND_ROUTER_DIR" ]]; then
    echo "[ERROR] Router Claude frontend directory not found: $TB_FUSION_ROUND_ROUTER_DIR" >&2
    return 2
  fi
  if [[ ! -f "$TB_FUSION_ROUND_ROUTER_DIR/subagent_barrier_gate.py" || ! -d "$TB_FUSION_ROUND_ROUTER_DIR/templates" ]]; then
    echo "[ERROR] Router Claude frontend is incomplete: $TB_FUSION_ROUND_ROUTER_DIR" >&2
    return 2
  fi
  if [[ -z "$TB_FUSION_TASK_FILE_SOURCE" || ! -f "$TB_FUSION_TASK_FILE_SOURCE" ]]; then
    echo "[ERROR] task contract source not found: ${TB_FUSION_TASK_FILE_SOURCE:-<empty>}" >&2
    return 2
  fi

  python3 "$MODEL_FUSION_DIR/harbor_worker_utils.py" \
    append-readonly-mounts "$mounts_json" \
    --mount "$TB_FUSION_ROUND_ROUTER_DIR" "$TB_FUSION_ROUND_ROUTER_MOUNT_PATH" \
    --mount "$TB_FUSION_TASK_FILE_SOURCE" "$TB_FUSION_TASK_FILE"
}
