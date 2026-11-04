#!/usr/bin/env bash
# openclaw-fleet.sh — Management tool for openclaw fleet instances.
# Usage: ./openclaw-fleet.sh <command> [selector] [options...]
#
# Selector formats:
#   all       All instances (default)
#   3         Single instance
#   1,3,5     Comma-separated list
#   2-5       Inclusive range
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

cfg="$PROJECT_DIR/config/fleet.env"
if [ -f "$cfg" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$cfg"
  set +a
fi

CONFIG_BASE="${CONFIG_BASE:-$HOME/openclaw-instances}"
WORKSPACE_BASE="${WORKSPACE_BASE:-$HOME/openclaw-workspaces}"
BASE_GW_PORT=18789
PORT_STEP="${PORT_STEP:-20}"
CONTAINER_NAME_PREFIX="${CONTAINER_NAME_PREFIX:-openclaw}"
DEFAULT_PORTS_OFFSET="${DEFAULT_PORTS_OFFSET:-0}"

# ── Docker command (use sudo on Linux if not in docker group) ──
DOCKER="docker"
if [ "$(uname)" = "Linux" ] && ! docker info &>/dev/null; then
  DOCKER="sudo docker"
fi
docker_compose() {
  $DOCKER compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# ── Discover instance count from .env ──
instance_count() {
  if [ ! -f "$ENV_FILE" ]; then
    echo 0
    return
  fi
  grep -c '^TOKEN_' "$ENV_FILE" 2>/dev/null || echo 0
}

svc_name() {
  echo "${CONTAINER_NAME_PREFIX}-$1"
}

# ── Parse selector into a list of instance numbers ──
parse_selector() {
  local sel="${1:-all}"
  local total
  total=$(instance_count)

  if [ "$total" -eq 0 ]; then
    echo "No instances found. Run ./setup.sh first." >&2
    exit 1
  fi

  case "$sel" in
    all)
      seq 1 "$total"
      ;;
    *-*)
      local from="${sel%-*}"
      local to="${sel#*-}"
      seq "$from" "$to"
      ;;
    *,*)
      echo "$sel" | tr ',' '\n'
      ;;
    *)
      echo "$sel"
      ;;
  esac
}

# ── Get gateway port for instance i ──
gw_port() {
  echo $((BASE_GW_PORT + DEFAULT_PORTS_OFFSET + ($1 - 1) * PORT_STEP))
}

# ── Get token for instance i ──
get_token() {
  local i="$1"
  if [ -f "$ENV_FILE" ]; then
    grep "^TOKEN_${i}=" "$ENV_FILE" | cut -d= -f2
  fi
}

# ── Commands ──

cmd_status() {
  local sel="${1:-all}"
  printf "%-14s %-8s %-10s %-18s %-6s %s\n" "INSTANCE" "HEALTH" "CPU" "MEM" "MEM%" "PORT"
  printf '%0.s─' {1..70}; echo

  for i in $(parse_selector "$sel"); do
    local svc
    svc=$(svc_name "$i")
    local port
    port=$(gw_port "$i")

    # Check if container is running
    if ! $DOCKER ps --format '{{.Names}}' | grep -q "^${svc}$"; then
      printf "%-14s %-8s %-10s %-18s %-6s %s\n" "$svc" "down" "-" "-" "-" ":${port}"
      continue
    fi

    # Health check
    local health http_code body status_from_json
    health="?"
    body=$(curl -sS --noproxy '*' --max-time 3 -w $'\n%{http_code}' "http://127.0.0.1:${port}/healthz" 2>/dev/null || true)
    http_code=$(printf '%s\n' "$body" | tail -n 1)
    body=$(printf '%s\n' "$body" | sed '$d')

    if [ "$http_code" = "200" ]; then
      status_from_json=""
      if command -v jq &>/dev/null; then
        status_from_json=$(printf '%s' "$body" | jq -r '.status // empty' 2>/dev/null || true)
      fi
      if [ -z "$status_from_json" ]; then
        status_from_json=$(printf '%s' "$body" | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || true)
      fi
      health="${status_from_json:-live}"
    elif [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
      health="http-${http_code}"
    fi

    # Stats
    local stats
    stats=$($DOCKER stats --no-stream --format "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" "$svc" 2>/dev/null || echo "-\t-\t-")
    local cpu mem memp
    cpu=$(echo "$stats" | cut -f1)
    mem=$(echo "$stats" | cut -f2)
    memp=$(echo "$stats" | cut -f3)

    printf "%-14s %-8s %-10s %-18s %-6s %s\n" "$svc" "$health" "$cpu" "$mem" "$memp" ":${port}"
  done
}

cmd_probe() {
  local sel="${1:-all}"
  source "$ENV_FILE"

  for i in $(parse_selector "$sel"); do
    local svc
    svc=$(svc_name "$i")
    echo "── ${svc} ──"
    local token
    token=$(get_token "$i")
    if [ -z "$token" ]; then
      echo "  No token found for instance $i"
      continue
    fi
    $DOCKER exec \
      -e OPENCLAW_GATEWAY_TOKEN="$token" \
      "$svc" \
      node dist/index.js channels status --probe 2>&1 || true
    echo
  done
}

cmd_logs() {
  local sel="${1:-all}"
  shift || true
  for i in $(parse_selector "$sel"); do
    local svc
    svc=$(svc_name "$i")
    echo "── ${svc} ──"
    docker_compose logs "$svc" "$@" 2>/dev/null || $DOCKER logs "$svc" "$@" 2>/dev/null || echo "  (not found)"
    echo
  done
}

cmd_start() {
  local sel="${1:-all}"
  for i in $(parse_selector "$sel"); do
    docker_compose start "$(svc_name "$i")"
  done
}

cmd_stop() {
  local sel="${1:-all}"
  for i in $(parse_selector "$sel"); do
    docker_compose stop "$(svc_name "$i")"
  done
}

cmd_restart() {
  local sel="${1:-all}"
  for i in $(parse_selector "$sel"); do
    docker_compose restart "$(svc_name "$i")"
  done
}

cmd_token() {
  local sel="${1:-all}"
  for i in $(parse_selector "$sel"); do
    local token
    token=$(get_token "$i")
    printf "%-14s %s\n" "$(svc_name "$i")" "${token:-(not found)}"
  done
}

cmd_config() {
  local n="${1:?Usage: openclaw-fleet.sh config <N>}"
  local cfg="$CONFIG_BASE/$n/openclaw.json"
  if [ -f "$cfg" ]; then
    cat "$cfg"
  else
    echo "Config not found: $cfg" >&2
    exit 1
  fi
}

cmd_config_set() {
  local sel="${1:?Usage: openclaw-fleet.sh config-set <sel> '<jq-expression>'}"
  local expr="${2:?Usage: openclaw-fleet.sh config-set <sel> '<jq-expression>'}"

  if ! command -v jq &>/dev/null; then
    echo "jq is required for config-set" >&2
    exit 1
  fi

  for i in $(parse_selector "$sel"); do
    local cfg="$CONFIG_BASE/$i/openclaw.json"
    if [ -f "$cfg" ]; then
      local tmp
      tmp=$(mktemp)
      jq "$expr" "$cfg" > "$tmp" && mv "$tmp" "$cfg"
      echo "Updated: $cfg"
    else
      echo "Skipped (not found): $cfg" >&2
    fi
  done
}

cmd_exec() {
  local n="${1:?Usage: openclaw-fleet.sh exec <N> [command...]}"
  shift
  local cmd=("${@:-bash}")
  $DOCKER exec -it "$(svc_name "$n")" "${cmd[@]}"
}

cmd_workspace() {
  local sel="${1:-all}"
  for i in $(parse_selector "$sel"); do
    local ws="$WORKSPACE_BASE/$i"
    if [ -d "$ws" ]; then
      local size
      size=$(du -sh "$ws" 2>/dev/null | cut -f1)
      printf "%-14s %s  (%s)\n" "$(svc_name "$i")" "$size" "$ws"
    else
      printf "%-14s (no workspace dir)\n" "$(svc_name "$i")"
    fi
  done
}

cmd_clean_workspace() {
  local sel="${1:?Usage: openclaw-fleet.sh clean-workspace <sel>}"
  local instances
  instances=$(parse_selector "$sel")

  echo "This will delete workspace contents for: $(echo $instances | tr '\n' ' ')"
  read -rp "Continue? [y/N] " confirm
  if [[ "$confirm" != [yY] ]]; then
    echo "Aborted."
    return
  fi

  for i in $instances; do
    local ws="$WORKSPACE_BASE/$i"
    if [ -d "$ws" ]; then
      rm -rf "${ws:?}"/*
      echo "Cleaned: $ws"
    fi
  done
}

cmd_scale() {
  local n="${1:?Usage: openclaw-fleet.sh scale <N>}"
  echo "Scaling to $n instances..."
  docker_compose down 2>/dev/null || true
  "$SCRIPT_DIR/setup.sh" "$n"
  docker_compose up -d
}

cmd_df() {
  echo "=== Config directories ==="
  du -sh "$CONFIG_BASE" 2>/dev/null || echo "(not found)"
  echo ""
  echo "=== Workspace directories ==="
  du -sh "$WORKSPACE_BASE" 2>/dev/null || echo "(not found)"
  echo ""
  echo "=== Docker system ==="
  $DOCKER system df 2>/dev/null || true
}

cmd_plugin_status() {
  local sel="${1:-all}"
  source "$ENV_FILE"

  for i in $(parse_selector "$sel"); do
    local svc
    svc=$(svc_name "$i")
    echo "── ${svc} ──"
    local token
    token=$(get_token "$i")
    if [ -z "$token" ]; then
      echo "  No token found for instance $i"
      continue
    fi
    $DOCKER exec \
      -e OPENCLAW_GATEWAY_TOKEN="$token" \
      "$svc" \
      node dist/index.js plugins list 2>&1 || echo "  (failed — is the container running?)"
    echo
  done
}

cmd_help() {
  cat <<'USAGE'
Usage: ./openclaw-fleet.sh <command> [selector] [options...]

Selector: all | 3 | 1,3,5 | 2-5

Commands:
  status  [sel]                  Health, CPU/mem stats, ports
  probe   [sel]                  WebSocket gateway probe (authenticated)
  logs    [sel] [--tail N] [-f]  Container logs
  start   [sel]                  Start containers
  stop    [sel]                  Stop containers
  restart [sel]                  Restart containers
  token   [sel]                  Show gateway tokens
  config  <N>                    Show openclaw.json for instance N
  config-set <sel> '<jq-expr>'   Bulk-edit configs (requires jq)
  exec    <N> [cmd...]           Exec into container
  workspace [sel]                Show workspace disk usage
  clean-workspace <sel>          Wipe workspace contents
  scale   <N>                    Resize fleet (down + regenerate + up)
  plugin-status [sel]            Show installed plugins
  df                             Disk overview
  help                           Show this help
USAGE
}

# ── Dispatch ──
CMD="${1:-help}"
shift || true

case "$CMD" in
  status)          cmd_status "$@" ;;
  probe)           cmd_probe "$@" ;;
  logs)            cmd_logs "$@" ;;
  start)           cmd_start "$@" ;;
  stop)            cmd_stop "$@" ;;
  restart)         cmd_restart "$@" ;;
  token)           cmd_token "$@" ;;
  config)          cmd_config "$@" ;;
  config-set)      cmd_config_set "$@" ;;
  exec)            cmd_exec "$@" ;;
  workspace)       cmd_workspace "$@" ;;
  clean-workspace) cmd_clean_workspace "$@" ;;
  scale)           cmd_scale "$@" ;;
  plugin-status)   cmd_plugin_status "$@" ;;
  df)              cmd_df "$@" ;;
  help|--help|-h)  cmd_help ;;
  *)               echo "Unknown command: $CMD" >&2; cmd_help; exit 1 ;;
esac
