#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"
if [[ "${1:-}" == "--help" || "$#" == 0 ]]; then
  cat <<'HELP'
Usage: Tasks/AgentsLastExam/run.sh --all [--workers N] [--output DIR] [-- HARBOR_OPTIONS]
       Tasks/AgentsLastExam/run.sh --domain visual_media --dry-run
       Tasks/AgentsLastExam/run.sh --task DOMAIN/TASK --agent module:Class

Run the full ALE CPU benchmark (Linux and Windows; GPU tasks excluded).
Run setup.sh first and configure HARBOR_ALE_IMAGE_MAP. Use --os linux|windows
to select one OS. The default ale-command agent uses image-provided entrypoints.
Pass native Harbor options after --. Example: -- --max-retries 2
Resume with Harbor: <ALE_ENV>/bin/harbor jobs resume --job-path DIR.
HELP
  exit 0
fi
export HARBOR_ALE_CACHE_DIR="${HARBOR_ALE_CACHE_DIR:-${AGENT_FLEET_CACHE_DIR:-$HOME/.cache/agent-fleet}/ale}"
HARBOR_ALE_ENV_DIR="${HARBOR_ALE_ENV_DIR:-$HARBOR_ALE_CACHE_DIR/venv}"
if [[ ! -x "$HARBOR_ALE_ENV_DIR/bin/python" ]]; then
  echo '[ERROR] ALE host environment is missing; run Tasks/AgentsLastExam/setup.sh.' >&2
  exit 1
fi
export HARBOR_KUBEVIRT_GUEST_PROTOCOL="${HARBOR_KUBEVIRT_GUEST_PROTOCOL-ale}"
export OPENAI_API_KEY="${OPENAI_API_KEY-${API_KEY:-}}"
if [[ "${OPENAI_BASE_URL+x}" != "x" && -n "${BASE_URL:-}" ]]; then
  export OPENAI_BASE_URL="${BASE_URL%/}/v1"
fi
export PYTHONPATH="$REPO_ROOT:$SCRIPT_DIR:$REPO_ROOT/Agents/utils/common/Harbor${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
exec "$HARBOR_ALE_ENV_DIR/bin/python" -m ale_adapter.launch --cache "$HARBOR_ALE_CACHE_DIR" "$@"
