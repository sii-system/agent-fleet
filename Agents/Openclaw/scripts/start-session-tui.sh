#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_DIR/../.." && pwd)"
# shellcheck source=../../../scripts/prerequisites.sh
source "$REPO_ROOT/scripts/prerequisites.sh"
agent_fleet_prerequisite_init_path
agent_fleet_prerequisite_init_runtime
ENV_FILE="$PROJECT_DIR/.env"
RUNTIME_DIR="${OPENCLAW_TUI_RUNTIME_DIR:-$PROJECT_DIR/.runtime}"
LAYOUT_FILE="$RUNTIME_DIR/session-tui-layout.kdl"
TOTAL_WORKERS="${TOTAL_WORKERS:-}"
ZELLIJ_BIN="${ZELLIJ_BIN:-zellij}"

ensure_zellij_web_sharing_config() {
  local config_file="${ZELLIJ_CONFIG_FILE:-$HOME/.config/zellij/config.kdl}"
  mkdir -p "$(dirname "$config_file")"
  if [[ -f "$config_file" ]] && grep -qE '^[[:space:]]*web_sharing[[:space:]]+' "$config_file"; then
    sed -i -E 's/^[[:space:]]*web_sharing[[:space:]].*$/web_sharing "on"/' "$config_file"
  else
    printf '\nweb_sharing "on"\n' >> "$config_file"
  fi
}

instance_count() {
  if [[ -n "${TOTAL_WORKERS:-}" ]]; then
    printf '%s\n' "$TOTAL_WORKERS"
    return 0
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    printf '0\n'
    return 0
  fi
  grep -c '^TOKEN_' "$ENV_FILE" 2>/dev/null || printf '0\n'
}

mkdir -p "$RUNTIME_DIR"
TOTAL_WORKERS="$(instance_count)"
if [[ "$TOTAL_WORKERS" -le 0 ]]; then
  echo "No OpenClaw instances found. Run ./Agents/Openclaw/scripts/setup.sh first." >&2
  exit 1
fi

export TOTAL_WORKERS LAYOUT_FILE
"$SCRIPT_DIR/gen_session_zellij_layout.sh" "$LAYOUT_FILE" >/dev/null

if [[ $# -gt 0 ]]; then
  exec "$@"
fi

if ! command -v "$ZELLIJ_BIN" >/dev/null 2>&1; then
  echo "zellij is required to launch the session TUI" >&2
  exit 1
fi

ensure_zellij_web_sharing_config

cd "$SCRIPT_DIR"
exec "$ZELLIJ_BIN" --layout "$LAYOUT_FILE"
