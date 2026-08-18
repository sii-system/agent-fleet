#!/usr/bin/env bash
set -euo pipefail

REAL_OPIK_BIN="${OPENROUTER_REAL_HARBOR_OPIK_BIN:?missing real Opik CLI path}"
OPENROUTER_DIR="${OPENROUTER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [[ "$REAL_OPIK_BIN" == "$0" ]]; then
  echo "[ERROR] OpenRouter Opik proxy points to itself" >&2
  exit 2
fi

inject=0
if [[ "${1:-}" == "harbor" && "${2:-}" == "run" ]]; then
  inject=1
  for arg in "$@"; do
    [[ "$arg" != "--help" && "$arg" != "-h" ]] || inject=0
  done
fi
[[ "$inject" == "1" ]] || exec "$REAL_OPIK_BIN" "$@"

: "${OPENROUTER_WHEEL:?missing Router wheel}"
: "${OPENROUTER_CONFIG:?missing Router config}"
: "${OPENROUTER_VERSION:?missing Router version}"
[[ -f "$OPENROUTER_WHEEL" ]] || { echo "[ERROR] Router wheel not found" >&2; exit 2; }
[[ -f "$OPENROUTER_CONFIG" ]] || { echo "[ERROR] Router config not found" >&2; exit 2; }

args=("$@")
gateway_host="$(
  python3 - "${TB_ANTHROPIC_BASE_URL:-${BASE_URL:-}}" <<'PY'
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
        [[ ",$value," == *",$gateway_host,"* ]] || args[$((i + 1))]="NO_PROXY=${value:+$value,}$gateway_host"
        ;;
      no_proxy=*)
        found_lower=1
        value="${args[$((i + 1))]#no_proxy=}"
        [[ ",$value," == *",$gateway_host,"* ]] || args[$((i + 1))]="no_proxy=${value:+$value,}$gateway_host"
        ;;
    esac
  done
  [[ "$found_upper" == "1" ]] || args+=(--ae "NO_PROXY=$gateway_host")
  [[ "$found_lower" == "1" ]] || args+=(--ae "no_proxy=$gateway_host")
fi

mount_index=-1
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--mounts-json" ]]; then
    ((i + 1 < ${#args[@]})) || { echo "[ERROR] --mounts-json missing value" >&2; exit 2; }
    mount_index=$((i + 1))
    break
  fi
done
mounts_json="[]"
((mount_index < 0)) || mounts_json="${args[$mount_index]}"
mounts_json="$(
  python3 "$OPENROUTER_DIR/../harbor_worker_utils.py" append-readonly-mounts \
    "$mounts_json" \
    --mount "$OPENROUTER_WHEEL" "$OPENROUTER_WHEEL_MOUNT_PATH" \
    --mount "$OPENROUTER_CONFIG" "$OPENROUTER_CONFIG_MOUNT_PATH"
)"
if ((mount_index < 0)); then
  args+=(--mounts-json "$mounts_json")
else
  args[$mount_index]="$mounts_json"
fi

args+=(
  --ae "OPENROUTER_ENABLED=1"
  --ae "OPENROUTER_VERSION=$OPENROUTER_VERSION"
  --ae "OPENROUTER_WHEEL_PATH=$OPENROUTER_WHEEL_MOUNT_PATH"
  --ae "OPENROUTER_CONFIG_PATH=$OPENROUTER_CONFIG_MOUNT_PATH"
  --ae "OPENROUTER_ARTIFACT_ROOT=${OPENROUTER_ARTIFACT_ROOT:-/logs/agent/router}"
  --ae "OPENROUTER_SUMMARY_PATH=${OPENROUTER_SUMMARY_PATH:-/logs/agent/router-run-summary.json}"
)
if [[ "${MODEL_FUSION_PROXY_RENDER_ONLY:-0}" == "1" ]]; then
  rendered_command="$(
    python3 "$OPENROUTER_DIR/../harbor_worker_utils.py" \
      render-command "$REAL_OPIK_BIN" "${args[@]}"
  )"
  printf '[openrouter] proxy command: %s\n' "$rendered_command"
  exit 0
fi
exec "$REAL_OPIK_BIN" "${args[@]}"
