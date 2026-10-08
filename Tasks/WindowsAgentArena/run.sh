#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

if [[ "${1:-}" == "--help" || "$#" == 0 ]]; then
  cat <<'HELP'
Usage: Tasks/WindowsAgentArena/run.sh --all [--workers N] [--output DIR] [-- HARBOR_OPTIONS]
       Tasks/WindowsAgentArena/run.sh --domain chrome --dry-run
       Tasks/WindowsAgentArena/run.sh --task DOMAIN/ID --agent module:factory

Run the official WAA (154 tasks) or WAA-V2 (141 tasks) Harbor benchmark using PC-Agent or a
custom reset/predict agent. Run setup.sh first. See Tasks/WindowsAgentArena/README.md
for the prepared Windows image, cluster prerequisites, custom agents and results.
Select --benchmark waa or waa-v2 (default). Pass native Harbor options after --.
Example: -- --ak max_steps=30 --max-retries 2
Resume with Harbor: <WAA_ENV>/bin/harbor jobs resume --job-path DIR.
HELP
  exit 0
fi

HARBOR_WAA_CACHE_DIR="${HARBOR_WAA_CACHE_DIR:-${AGENT_FLEET_CACHE_DIR:-$HOME/.cache/agent-fleet}/waa}"
HARBOR_WAA_ENV_DIR="${HARBOR_WAA_ENV_DIR:-$HARBOR_WAA_CACHE_DIR/venv}"
if [[ ! -x "$HARBOR_WAA_ENV_DIR/bin/python" ]]; then
  echo '[ERROR] WAA host environment is missing; run Tasks/WindowsAgentArena/setup.sh.' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/Agents/utils/common/Harbor:$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
# Canonical fleet settings are defaults; explicit OpenAI values (including empty)
# retain precedence for custom agents and alternative gateways.
export OPENAI_API_KEY="${OPENAI_API_KEY-${API_KEY:-}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL-${BASE_URL:-https://api.openai.com/v1}}"
export HARBOR_KUBEVIRT_GUEST_PROTOCOL="${HARBOR_KUBEVIRT_GUEST_PROTOCOL-waa}"
export HARBOR_KUBEVIRT_EXTRA_PORTS="${HARBOR_KUBEVIRT_EXTRA_PORTS-9222,8080}"
exec "$HARBOR_WAA_ENV_DIR/bin/python" -m waa_benchmark.launch \
  --cache "$HARBOR_WAA_CACHE_DIR" "$@"
