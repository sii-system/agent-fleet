#!/usr/bin/env bash
set -euo pipefail

# Scoped proxy for the Opik CLI. run_one_tb21_task.sh selects this executable
# through HARBOR_OPIK_BIN; ordinary Harbor launches continue to call the real
# binary directly and never pass through model-fusion code.
REAL_OPIK_BIN="${MODEL_FUSION_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
MODEL_FUSION_DIR="${MODEL_FUSION_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
. "$MODEL_FUSION_DIR/proxy_common.sh"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] model-fusion Opik proxy points to itself" >&2
  exit 2
fi

# Version and help probes must remain transparent so the shared pinned-runner
# validation sees the real CLI without requiring a prepared fusion task.
if ! model_fusion_proxy_is_injectable "$@"; then
  exec "$REAL_OPIK_BIN" "$@"
fi

: "${HARBOR_FUSION_ROUND_ROUTER_DIR:?missing Router Claude frontend path}"
: "${HARBOR_FUSION_ROUND_ROUTER_MOUNT_PATH:?missing Router mount target}"
: "${HARBOR_FUSION_TASK_FILE_SOURCE:?missing task contract source}"
: "${HARBOR_FUSION_TASK_FILE:?missing task contract mount target}"

if [[ ! -f "$HARBOR_FUSION_ROUND_ROUTER_DIR/subagent_barrier_gate.py" || ! -d "$HARBOR_FUSION_ROUND_ROUTER_DIR/templates" ]]; then
  echo "[ERROR] Router Claude frontend is incomplete: $HARBOR_FUSION_ROUND_ROUTER_DIR" >&2
  exit 2
fi
if [[ ! -f "$HARBOR_FUSION_TASK_FILE_SOURCE" ]]; then
  echo "[ERROR] task contract source not found: $HARBOR_FUSION_TASK_FILE_SOURCE" >&2
  exit 2
fi

args=("$@")

# The host may use an outbound proxy, but Anthropic-compatible campus/private
# gateways must be reached directly from the task container. The shared Harbor
# command already carries NO_PROXY for Opik and wheel hosts; retain that value
# and add the model gateway host for this scoped integration.
model_fusion_proxy_append_gateway_no_proxy \
  "$MODEL_FUSION_DIR/../harbor_shell_utils.py" \
  "${HARBOR_ANTHROPIC_BASE_URL:-${BASE_URL:-}}"
model_fusion_proxy_merge_readonly_mounts \
  "$MODEL_FUSION_DIR/harbor_worker_utils.py" \
  --mount "$HARBOR_FUSION_ROUND_ROUTER_DIR" "$HARBOR_FUSION_ROUND_ROUTER_MOUNT_PATH" \
  --mount "$HARBOR_FUSION_TASK_FILE_SOURCE" "$HARBOR_FUSION_TASK_FILE"

args+=(
  --ae "HARBOR_CLAUDE_CODE_AGENTS_JSON=${HARBOR_CLAUDE_CODE_AGENTS_JSON:-}"
  --ae "HARBOR_FUSION_ROUND_GATE=${HARBOR_FUSION_ROUND_GATE:-0}"
  --ae "HARBOR_FUSION_ROUND_GATE_PATH=${HARBOR_FUSION_ROUND_GATE_PATH:-}"
  --ae "HARBOR_FUSION_ROUND_GATE_MODE=${HARBOR_FUSION_ROUND_GATE_MODE:-mid-turn-fusion}"
  --ae "HARBOR_FUSION_TASK_FILE=$HARBOR_FUSION_TASK_FILE"
  --ae "FUSION_TASK_FILE=$HARBOR_FUSION_TASK_FILE"
  --ae "SPAN_FORCE_MODE=${SPAN_FORCE_MODE:-mid-turn-fusion}"
  --ae "SPAN_FORCE_FUSION=${SPAN_FORCE_FUSION:-1}"
  --ae "SPAN_GATE_STATE_PATH=${SPAN_GATE_STATE_PATH:-}"
  --ae "SPAN_MID_TURN_ARTIFACT_ROOT=${SPAN_MID_TURN_ARTIFACT_ROOT:-}"
  --ae "SPAN_HOOK_REASON_MAX_BYTES=${SPAN_HOOK_REASON_MAX_BYTES:-7000}"
  --ae "SPAN_PANEL_MODELS=${SPAN_PANEL_MODELS:-}"
  --ae "SPAN_PANEL_COUNT=${SPAN_PANEL_COUNT:-}"
  --ae "SPAN_MID_TURN_MAX_FUSIONS_PER_TASK=${HARBOR_FUSION_MAX_FUSIONS_PER_TASK:-1}"
  --ae "SPAN_MID_TURN_PANEL_CALL_BUDGET=${HARBOR_FUSION_PANEL_CALL_BUDGET:-}"
)

exec "$REAL_OPIK_BIN" "${args[@]}"
