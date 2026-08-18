#!/usr/bin/env bash
set -euo pipefail

# Scoped proxy for the Opik CLI. run_one_tb21_task.sh selects this executable
# through HARBOR_OPIK_BIN; ordinary Harbor launches continue to call the real
# binary directly and never pass through model-fusion code.
REAL_OPIK_BIN="${MODEL_FUSION_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
MODEL_FUSION_DIR="${MODEL_FUSION_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] model-fusion Opik proxy points to itself" >&2
  exit 2
fi

# Version and help probes must remain transparent so the shared pinned-runner
# validation sees the real CLI without requiring a prepared fusion task.
inject_fusion=0
if [[ "${1:-}" == "harbor" && "${2:-}" == "run" ]]; then
  inject_fusion=1
  for arg in "$@"; do
    if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
      inject_fusion=0
      break
    fi
  done
fi
if [[ "$inject_fusion" != "1" ]]; then
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
gateway_host="$(
  python3 - "${HARBOR_ANTHROPIC_BASE_URL:-${BASE_URL:-}}" <<'PY'
from urllib.parse import urlparse
import sys

print(urlparse(sys.argv[1]).hostname or "")
PY
)"
if [[ -n "$gateway_host" ]]; then
  found_upper=0
  found_lower=0
  for ((i = 0; i + 1 < ${#args[@]}; i++)); do
    [[ "${args[$i]}" == "--ae" ]] || continue
    case "${args[$((i + 1))]}" in
      NO_PROXY=*)
        found_upper=1
        value="${args[$((i + 1))]#NO_PROXY=}"
        [[ ",$value," == *",$gateway_host,"* ]] \
          || args[$((i + 1))]="NO_PROXY=${value:+$value,}$gateway_host"
        ;;
      no_proxy=*)
        found_lower=1
        value="${args[$((i + 1))]#no_proxy=}"
        [[ ",$value," == *",$gateway_host,"* ]] \
          || args[$((i + 1))]="no_proxy=${value:+$value,}$gateway_host"
        ;;
    esac
  done
  [[ "$found_upper" == "1" ]] || args+=(--ae "NO_PROXY=$gateway_host")
  [[ "$found_lower" == "1" ]] || args+=(--ae "no_proxy=$gateway_host")
fi

mount_value_index=-1
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--mounts-json" ]]; then
    if ((i + 1 >= ${#args[@]})); then
      echo "[ERROR] --mounts-json is missing its value" >&2
      exit 2
    fi
    mount_value_index=$((i + 1))
    break
  fi
done

mounts_json="[]"
if ((mount_value_index >= 0)); then
  mounts_json="${args[$mount_value_index]}"
fi
mounts_json="$(
  python3 "$MODEL_FUSION_DIR/harbor_worker_utils.py" \
    append-readonly-mounts "$mounts_json" \
    --mount "$HARBOR_FUSION_ROUND_ROUTER_DIR" "$HARBOR_FUSION_ROUND_ROUTER_MOUNT_PATH" \
    --mount "$HARBOR_FUSION_TASK_FILE_SOURCE" "$HARBOR_FUSION_TASK_FILE"
)"
if ((mount_value_index >= 0)); then
  args[$mount_value_index]="$mounts_json"
else
  args+=(--mounts-json "$mounts_json")
fi

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
