#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"
export HARBOR_ALE_CACHE_DIR="${HARBOR_ALE_CACHE_DIR:-${AGENT_FLEET_CACHE_DIR:-$HOME/.cache/agent-fleet}/ale}"
export UV_PROJECT_ENVIRONMENT="${HARBOR_ALE_ENV_DIR:-$HARBOR_ALE_CACHE_DIR/venv}"
export PYTHONPATH="$REPO_ROOT:$SCRIPT_DIR:$REPO_ROOT/Agents/utils/common/Harbor${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
uv sync --python 3.12 --project "$SCRIPT_DIR"
exec "$UV_PROJECT_ENVIRONMENT/bin/python" -m ale_adapter.prepare \
  --cache "$HARBOR_ALE_CACHE_DIR" --lockfile "$SCRIPT_DIR/uv.lock" "$@"
