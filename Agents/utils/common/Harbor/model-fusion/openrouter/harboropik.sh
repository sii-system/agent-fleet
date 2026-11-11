#!/usr/bin/env bash
set -euo pipefail

REAL_OPIK_BIN="${OPENROUTER_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
OPENROUTER_DIR="${OPENROUTER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
. "$OPENROUTER_DIR/../proxy_common.sh"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] OpenRouter Opik proxy points to itself" >&2
  exit 2
fi

if ! model_fusion_proxy_is_injectable "$@"; then
  exec "$REAL_OPIK_BIN" "$@"
fi

: "${OPENROUTER_WHEEL:?missing Router wheel}"
: "${OPENROUTER_CONFIG:?missing Router config}"
: "${OPENROUTER_VERSION:?missing Router version}"
[[ -f "$OPENROUTER_WHEEL" ]] || { echo "[ERROR] Router wheel not found" >&2; exit 2; }
[[ -f "$OPENROUTER_CONFIG" ]] || { echo "[ERROR] Router config not found" >&2; exit 2; }

args=("$@")
model_fusion_proxy_append_gateway_no_proxy \
  "$OPENROUTER_DIR/../../harbor_shell_utils.py" \
  "${HARBOR_ANTHROPIC_BASE_URL:-${BASE_URL:-}}"
model_fusion_proxy_merge_readonly_mounts \
  "$OPENROUTER_DIR/../harbor_worker_utils.py" \
  --mount "$OPENROUTER_WHEEL" "$OPENROUTER_WHEEL_MOUNT_PATH" \
  --mount "$OPENROUTER_CONFIG" "$OPENROUTER_CONFIG_MOUNT_PATH"

args+=(
  --ae "OPENROUTER_ENABLED=1"
  --ae "OPENROUTER_VERSION=$OPENROUTER_VERSION"
  --ae "OPENROUTER_WHEEL_PATH=$OPENROUTER_WHEEL_MOUNT_PATH"
  --ae "OPENROUTER_CONFIG_PATH=$OPENROUTER_CONFIG_MOUNT_PATH"
  --ae "OPENROUTER_ARTIFACT_ROOT=${OPENROUTER_ARTIFACT_ROOT:-/logs/agent/router}"
  --ae "OPENROUTER_SUMMARY_PATH=${OPENROUTER_SUMMARY_PATH:-/logs/agent/router-run-summary.json}"
)
model_fusion_proxy_render_or_exec \
  "openrouter" "$REAL_OPIK_BIN" "$OPENROUTER_DIR/../harbor_worker_utils.py"
