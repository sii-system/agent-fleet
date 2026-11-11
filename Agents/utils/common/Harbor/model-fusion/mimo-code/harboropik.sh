#!/usr/bin/env bash
set -euo pipefail

REAL_OPIK_BIN="${MIMO_CODE_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
MIMO_CODE_DIR="${MIMO_CODE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
. "$MIMO_CODE_DIR/../proxy_common.sh"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] MimoCode Opik proxy points to itself" >&2
  exit 2
fi

if ! model_fusion_proxy_is_injectable "$@"; then
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

model_fusion_proxy_append_gateway_no_proxy \
  "$MIMO_CODE_DIR/../../harbor_shell_utils.py" \
  "${HARBOR_ANTHROPIC_BASE_URL:-${BASE_URL:-}}"
model_fusion_proxy_merge_readonly_mounts \
  "$MIMO_CODE_DIR/../harbor_worker_utils.py" \
  --mount "$MIMO_ROUTER_WHEEL" "$MIMO_ROUTER_WHEEL_MOUNT_PATH" \
  --mount "$MIMO_ROUTER_CONFIG" "$MIMO_ROUTER_CONFIG_MOUNT_PATH"

args+=(
  --ae "MIMO_ROUTER_ENABLED=1"
  --ae "MIMO_ROUTER_PIPELINE=${MIMO_ROUTER_PIPELINE:-mimo_max}"
  --ae "MIMO_ROUTER_VERSION=$MIMO_ROUTER_VERSION"
  --ae "MIMO_ROUTER_WHEEL_PATH=$MIMO_ROUTER_WHEEL_MOUNT_PATH"
  --ae "MIMO_ROUTER_CONFIG_PATH=$MIMO_ROUTER_CONFIG_MOUNT_PATH"
  --ae "MIMO_ROUTER_ARTIFACT_ROOT=${MIMO_ROUTER_ARTIFACT_ROOT:-/logs/agent/router}"
  --ae "MIMO_ROUTER_SUMMARY_PATH=${MIMO_ROUTER_SUMMARY_PATH:-/logs/agent/router-run-summary.json}"
)

model_fusion_proxy_render_or_exec \
  "mimo-code" "$REAL_OPIK_BIN" "$MIMO_CODE_DIR/../harbor_worker_utils.py"
