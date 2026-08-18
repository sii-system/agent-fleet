#!/usr/bin/env bash
set -euo pipefail

REAL_OPIK_BIN="${MIMO_CODE_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
MIMO_CODE_DIR="${MIMO_CODE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] MimoCode Opik proxy points to itself" >&2
  exit 2
fi

inject_mimo=0
if [[ "${1:-}" == "harbor" && "${2:-}" == "run" ]]; then
  inject_mimo=1
  for arg in "$@"; do
    if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
      inject_mimo=0
      break
    fi
  done
fi
if [[ "$inject_mimo" != "1" ]]; then
  exec "$REAL_OPIK_BIN" "$@"
fi

: "${MIMO_ROUTER_WHEEL:?missing Router wheel}"
: "${MIMO_ROUTER_CONFIG:?missing Router config}"
: "${MIMO_ROUTER_VERSION:?missing Router version}"
[[ -f "$MIMO_ROUTER_WHEEL" ]] || {
  echo "[ERROR] Router wheel not found: $MIMO_ROUTER_WHEEL" >&2
  exit 2
}
[[ -f "$MIMO_ROUTER_CONFIG" ]] || {
  echo "[ERROR] Router config not found: $MIMO_ROUTER_CONFIG" >&2
  exit 2
}

args=("$@")

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
    ((i + 1 < ${#args[@]})) || {
      echo "[ERROR] --mounts-json is missing its value" >&2
      exit 2
    }
    mount_value_index=$((i + 1))
    break
  fi
done

mounts_json="[]"
if ((mount_value_index >= 0)); then
  mounts_json="${args[$mount_value_index]}"
fi
mounts_json="$(
  python3 "$MIMO_CODE_DIR/../harbor_worker_utils.py" \
    append-readonly-mounts "$mounts_json" \
    --mount "$MIMO_ROUTER_WHEEL" "$MIMO_ROUTER_WHEEL_MOUNT_PATH" \
    --mount "$MIMO_ROUTER_CONFIG" "$MIMO_ROUTER_CONFIG_MOUNT_PATH"
)"
if ((mount_value_index >= 0)); then
  args[$mount_value_index]="$mounts_json"
else
  args+=(--mounts-json "$mounts_json")
fi

args+=(
  --ae "MIMO_ROUTER_ENABLED=1"
  --ae "MIMO_ROUTER_PIPELINE=${MIMO_ROUTER_PIPELINE:-mimo_max}"
  --ae "MIMO_ROUTER_VERSION=$MIMO_ROUTER_VERSION"
  --ae "MIMO_ROUTER_WHEEL_PATH=$MIMO_ROUTER_WHEEL_MOUNT_PATH"
  --ae "MIMO_ROUTER_CONFIG_PATH=$MIMO_ROUTER_CONFIG_MOUNT_PATH"
  --ae "MIMO_ROUTER_ARTIFACT_ROOT=${MIMO_ROUTER_ARTIFACT_ROOT:-/logs/agent/router}"
  --ae "MIMO_ROUTER_SUMMARY_PATH=${MIMO_ROUTER_SUMMARY_PATH:-/logs/agent/router-run-summary.json}"
)

if [[ "${MODEL_FUSION_PROXY_RENDER_ONLY:-0}" == "1" ]]; then
  rendered_command="$(
    python3 "$MIMO_CODE_DIR/../harbor_worker_utils.py" \
      render-command "$REAL_OPIK_BIN" "${args[@]}"
  )"
  printf '[mimo-code] proxy command: %s\n' "$rendered_command"
  exit 0
fi

exec "$REAL_OPIK_BIN" "${args[@]}"
