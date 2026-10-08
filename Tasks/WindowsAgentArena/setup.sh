#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

export HARBOR_WAA_CACHE_DIR="${HARBOR_WAA_CACHE_DIR:-${AGENT_FLEET_CACHE_DIR:-$HOME/.cache/agent-fleet}/waa}"
export UV_PROJECT_ENVIRONMENT="${HARBOR_WAA_ENV_DIR:-$HARBOR_WAA_CACHE_DIR/venv}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/Agents/utils/common/Harbor:$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
uv sync --python 3.12 --project "$SCRIPT_DIR"
exec "$UV_PROJECT_ENVIRONMENT/bin/python" -m waa_benchmark.prepare \
  --cache "$HARBOR_WAA_CACHE_DIR" --lockfile "$SCRIPT_DIR/uv.lock" "$@"
