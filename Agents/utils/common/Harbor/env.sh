#!/usr/bin/env bash
set -euo pipefail

# Load saved configuration before applying quick-start defaults.
# shellcheck source=env/bootstrap.sh
source "$(dirname "${BASH_SOURCE[0]}")/env/bootstrap.sh"

# Quick-start settings. Supply credentials through config.local.env or the shell.
AGENT="${AGENT:-claude-code}"       # claude-code, opencode, pi, or oracle
MODEL="${MODEL:-minimax2.7}"
BASE_URL="${BASE_URL:-}"             # Model API root, without /v1
API_KEY="${API_KEY:-xxx}"
# Registry: seta, terminalbench21, sweverify, or owner/name[@version].
# Local data: DATASET_NAME=auto and DATASET_PATH pointing to the task directory.
DATASET_NAME="${DATASET_NAME:-auto}"
DATASET_PATH="${DATASET_PATH:-/workspace/seta-env/Harbor-Dataset}"
TOTAL_WORKERS="${TOTAL_WORKERS:-10}"
# Optional: one-task canary and Opik tracing (empty URL disables upload).
MIN_TEST="${MIN_TEST:-0}"
OPIK_URL="${OPIK_URL:-}"

# shellcheck source=env/runtime.sh
source "$SCRIPT_DIR/env/runtime.sh"
