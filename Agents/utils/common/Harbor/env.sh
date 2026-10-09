#!/usr/bin/env bash
set -euo pipefail

# Load saved configuration before applying quick-start defaults.
# shellcheck source=env/bootstrap.sh
source "$(dirname "${BASH_SOURCE[0]}")/env/bootstrap.sh"

# Quick-start settings. Supply credentials through config.local.env or the shell.
AGENT="${AGENT:-claude-code}"       # claude-code, opencode, pi, or oracle
MODEL="${MODEL:-minimax2.7}"
BASE_URL="${BASE_URL:-}"             # Model API root, without /v1
API_KEY="${API_KEY-xxx}"
# Registry: seta, terminalbench21, sweverify, or owner/name[@version].
# Local data: DATASET_NAME=auto and DATASET_PATH pointing to the task directory.
DATASET_NAME="${DATASET_NAME:-auto}"
_HARBOR_DATASET_PATH_CONFIGURED=0
[[ -z "${DATASET_PATH:-}" ]] || _HARBOR_DATASET_PATH_CONFIGURED=1
if [[ "${HARBOR_CC_WEB_MCP_ENABLED:-0}" == "1" ]]; then
  # shellcheck source=../../web_search/env.sh
  source "$REPO_ROOT/Agents/utils/web_search/env.sh"
elif [[ "$DATASET_NAME" == "browsecomp" || "$DATASET_NAME" == "deepsearchqa" ]] &&
     [[ -z "${DATASET_PATH:-}" ]]; then
  echo "[ERROR] $DATASET_NAME requires DATASET_PATH or HARBOR_CC_WEB_MCP_ENABLED=1" >&2
  return 1
fi
DATASET_PATH="${DATASET_PATH:-/workspace/seta-env/Harbor-Dataset}"
TOTAL_WORKERS="${TOTAL_WORKERS:-10}"
# One Harbor process owns all benchmark trials; concurrency is HARBOR_N_CONCURRENT.
export HARBOR_NATIVE_CONCURRENCY="${HARBOR_NATIVE_CONCURRENCY:-0}"
# Optional: one-task canary and Opik tracing (empty URL disables upload).
MIN_TEST="${MIN_TEST:-0}"
OPIK_URL="${OPIK_URL:-}"

# shellcheck source=env/runtime.sh
source "$SCRIPT_DIR/env/runtime.sh"
