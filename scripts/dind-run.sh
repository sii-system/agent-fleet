#!/usr/bin/env bash
# Run scripts/run_fleet.sh inside a Docker-in-Docker container.
set -euo pipefail

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*" >&2; }
err() { echo "[ERROR] $*" >&2; }

usage() {
  cat <<'EOF'
Usage: scripts/dind-run.sh <run_fleet.sh args...>

Runs scripts/setup.sh and scripts/run_fleet.sh inside a privileged DinD
container. Docker registry mirrors default from config.env and can be
overridden in config.local.env:

  DIND_REGISTRY_MIRRORS=https://docker.m.daocloud.io,https://mirror.ccs.tencentyun.com
  scripts/dind-run.sh --taskset terminalbench21 --agent claude-code --workers 1

Useful overrides:
  DIND_NAME                 Container name (default: agent-fleet-dind)
  DIND_IMAGE                DinD runner image (default tag follows its build inputs; built locally if missing)
  DIND_IMAGE_DOCKERFILE     Dockerfile for default image build (default: scripts/dind/Dockerfile)
  DIND_BASE_IMAGE           Base image used when building default image
  DIND_UV_IMAGE             uv image used when building default image
  DIND_DOCKER_VOLUME        /var/lib/docker volume (default: <name>-docker)
  DIND_HOME_VOLUME          benchmark-user home volume (default: <name>-home)
  DIND_USER                 user for the benchmark launcher (default: agent)
  DIND_HOME_DIR             benchmark-user home path (default: /home/<DIND_USER>)
  DIND_USER_UID/GID         launcher uid/gid (default: current host user)
  DIND_PORTS                Comma-separated docker -p entries
  DIND_MOUNTS               Comma-separated extra docker -v entries
  DIND_DEFAULT_ADDRESS_POOLS Semicolon-separated dockerd pools, e.g. base=10.200.0.0/13,size=21
  DIND_BOOTSTRAP            always|missing|skip (default: missing)
  DIND_RECREATE             1 recreates the DinD container but preserves Docker storage
  DIND_RESET                1 recreates the DinD container and removes Docker storage
EOF
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

running_in_container() {
  local marker

  if [[ -n "${container:-}" || -n "${KUBERNETES_SERVICE_HOST:-}" ||
        -f /.dockerenv || -f /run/.containerenv ]]; then
    return 0
  fi
  if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --container --quiet; then
    return 0
  fi
  for marker in /proc/1/cgroup /proc/self/cgroup; do
    if [[ -r "$marker" ]] && grep -Eqs '(^|[/.-])(docker|containerd|kubepods|libpod|lxc|podman)([/.:_-]|$)' "$marker"; then
      return 0
    fi
  done
  return 1
}

if running_in_container; then
  warn "dind-run.sh cannot start DinD inside a container; running scripts/run_fleet.sh directly"
  exec "$SCRIPT_DIR/run_fleet.sh" "$@"
fi

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
fleet_args=("$@")

caller_env="$(export -p)"
if [[ -f "$REPO_ROOT/config.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$REPO_ROOT/config.env"
  set +a
fi
if [[ -f "$REPO_ROOT/config.local.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$REPO_ROOT/config.local.env"
  set +a
fi
eval "$caller_env"
unset caller_env

DIND_NAME="${DIND_NAME:-agent-fleet-dind}"
DIND_IMAGE_DOCKERFILE="${DIND_IMAGE_DOCKERFILE:-$REPO_ROOT/scripts/dind/Dockerfile}"
DIND_BASE_IMAGE="${DIND_BASE_IMAGE:-m.daocloud.io/docker.io/library/debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818}"
DIND_UV_IMAGE="${DIND_UV_IMAGE:-m.daocloud.io/ghcr.io/astral-sh/uv:0.11.28}"
DIND_RUNNER_REQUIREMENTS_FILE="$REPO_ROOT/Agents/utils/common/Harbor/runner-requirements.txt"
DIND_DOCKERD_ENTRYPOINT_FILE="$REPO_ROOT/scripts/dind/dockerd-entrypoint.sh"
for image_input in \
  "$DIND_IMAGE_DOCKERFILE" \
  "$DIND_DOCKERD_ENTRYPOINT_FILE" \
  "$DIND_RUNNER_REQUIREMENTS_FILE"; do
  if [[ ! -f "$image_input" ]]; then
    err "DinD image input not found: $image_input"
    exit 1
  fi
done
DIND_IMAGE_FINGERPRINT="$({
  printf '%s\n' "$DIND_BASE_IMAGE" "$DIND_UV_IMAGE"
  cat "$DIND_IMAGE_DOCKERFILE" "$DIND_DOCKERD_ENTRYPOINT_FILE" "$DIND_RUNNER_REQUIREMENTS_FILE"
} | sha256sum | cut -c1-12)"
DIND_DEFAULT_IMAGE="agent-fleet-dind:28-$DIND_IMAGE_FINGERPRINT"
DIND_IMAGE="${DIND_IMAGE:-$DIND_DEFAULT_IMAGE}"
DIND_DOCKER_VOLUME="${DIND_DOCKER_VOLUME:-${DIND_NAME}-docker}"
DIND_HOME_VOLUME="${DIND_HOME_VOLUME:-${DIND_NAME}-home}"
DIND_USER="${DIND_USER:-agent}"
DIND_HOME_DIR="${DIND_HOME_DIR:-/home/${DIND_USER}}"
DIND_USER_UID="${DIND_USER_UID:-$(id -u)}"
DIND_USER_GID="${DIND_USER_GID:-$(id -g)}"
DIND_BOOTSTRAP="${DIND_BOOTSTRAP:-missing}"
DIND_DAEMON_READY_TIMEOUT="${DIND_DAEMON_READY_TIMEOUT:-60}"
DIND_PORTS="${DIND_PORTS:-}"
DIND_MOUNTS="${DIND_MOUNTS:-}"
DIND_DEFAULT_ADDRESS_POOLS="${DIND_DEFAULT_ADDRESS_POOLS:-}"

url_hostname() {
  python3 - "$1" <<'PY'
from urllib.parse import urlparse
import sys

print(urlparse(sys.argv[1]).hostname or "")
PY
}

append_csv_unique() {
  local existing="$1"
  shift
  local item value result="" existing_value duplicate
  declare -a values=()
  declare -a seen_values=()
  if [[ -n "$existing" ]]; then
    IFS=',' read -r -a values <<< "$existing"
  else
    values=("")
  fi
  for item in "${values[@]}" "$@"; do
    value="$(trim "$item")"
    [[ -n "$value" ]] || continue
    duplicate=0
    if [[ "${#seen_values[@]}" -gt 0 ]]; then
      for existing_value in "${seen_values[@]}"; do
        if [[ "$existing_value" == "$value" ]]; then
          duplicate=1
          break
        fi
      done
    fi
    [[ "$duplicate" == "0" ]] || continue
    seen_values+=("$value")
    if [[ -n "$result" ]]; then
      result+=","
    fi
    result+="$value"
  done
  printf '%s' "$result"
}

no_proxy_value="$(append_csv_unique "${NO_PROXY:-${no_proxy:-}}" \
  127.0.0.1 localhost host.docker.internal \
  "$(url_hostname "$BASE_URL")" "$(url_hostname "${OPIK_URL:-}")")"
declare -a proxy_env=()
for optional in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
  if [[ -n "${!optional:-}" ]]; then
    proxy_env+=("$optional=${!optional}")
  fi
done
if [[ -n "$no_proxy_value" ]]; then
  proxy_env+=("NO_PROXY=$no_proxy_value" "no_proxy=$no_proxy_value")
fi

case "$DIND_BOOTSTRAP" in
  always|missing|skip)
    ;;
  *)
    err "DIND_BOOTSTRAP must be always, missing, or skip"
    exit 1
    ;;
esac
for required in BASE_URL API_KEY MODEL; do
  if [[ -z "${!required:-}" ]]; then
    err "$required is required; set it in config.local.env or the caller environment"
    exit 1
  fi
done

if ! command -v docker >/dev/null 2>&1; then
  err "docker command not found on host"
  exit 1
fi

if [[ "$DIND_IMAGE" == "$DIND_DEFAULT_IMAGE" ]] && ! docker image inspect "$DIND_IMAGE" >/dev/null 2>&1; then
  info "building DinD runner image: $DIND_IMAGE"
  docker build \
    --build-arg "DIND_BASE_IMAGE=$DIND_BASE_IMAGE" \
    --build-arg "UV_IMAGE=$DIND_UV_IMAGE" \
    -f "$DIND_IMAGE_DOCKERFILE" \
    -t "$DIND_IMAGE" \
    "$REPO_ROOT"
fi

registry_mirrors="${DIND_REGISTRY_MIRRORS:-}"
if [[ -z "$registry_mirrors" && -n "${DIND_REGISTRY_MIRROR:-}" ]]; then
  registry_mirrors="$DIND_REGISTRY_MIRROR"
fi
default_address_pools="$DIND_DEFAULT_ADDRESS_POOLS"

declare -a daemon_args=()
declare -a port_args=()
declare -a mount_args=()
if [[ -n "$registry_mirrors" ]]; then
  IFS=',' read -r -a raw_values <<< "$registry_mirrors"
  for raw in "${raw_values[@]}"; do
    value="$(trim "$raw")"
    [[ -n "$value" ]] || continue
    daemon_args+=("--registry-mirror=$value")
  done
fi
if [[ -n "$default_address_pools" ]]; then
  IFS=';' read -r -a raw_values <<< "$default_address_pools"
  for raw in "${raw_values[@]}"; do
    value="$(trim "$raw")"
    [[ -n "$value" ]] || continue
    daemon_args+=("--default-address-pool=$value")
  done
fi
if [[ -n "$DIND_PORTS" ]]; then
  IFS=',' read -r -a raw_values <<< "$DIND_PORTS"
  for raw in "${raw_values[@]}"; do
    value="$(trim "$raw")"
    [[ -n "$value" ]] || continue
    port_args+=("-p" "$value")
  done
fi
if [[ -n "$DIND_MOUNTS" ]]; then
  IFS=',' read -r -a raw_values <<< "$DIND_MOUNTS"
  for raw in "${raw_values[@]}"; do
    value="$(trim "$raw")"
    [[ -n "$value" ]] || continue
    mount_args+=("-v" "$value")
  done
fi

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fxq "$DIND_NAME"
}

container_running() {
  docker ps --format '{{.Names}}' | grep -Fxq "$DIND_NAME"
}

if [[ "${DIND_RESET:-0}" == "1" ]]; then
  docker rm -f "$DIND_NAME" >/dev/null 2>&1 || true
  docker volume rm "$DIND_DOCKER_VOLUME" >/dev/null 2>&1 || true
elif [[ "${DIND_RECREATE:-0}" == "1" ]]; then
  docker rm -f "$DIND_NAME" >/dev/null 2>&1 || true
fi

if ! container_exists; then
  info "creating DinD container: $DIND_NAME"
  docker_run_args=(
    run -d --privileged
    --name "$DIND_NAME"
    --label "agent-fleet.runner-image=$DIND_IMAGE"
    --label "agent-fleet.registry-mirrors=$registry_mirrors"
    --label "agent-fleet.default-address-pools=$default_address_pools"
    -e DOCKER_TLS_CERTDIR=
    -v "$DIND_DOCKER_VOLUME:/var/lib/docker"
    -v "$DIND_HOME_VOLUME:$DIND_HOME_DIR"
    -v "$REPO_ROOT:$REPO_ROOT"
  )
  for value in "${proxy_env[@]}"; do
    docker_run_args+=(-e "$value")
  done
  if [[ ${#mount_args[@]} -gt 0 ]]; then
    docker_run_args+=("${mount_args[@]}")
  fi
  if [[ ${#port_args[@]} -gt 0 ]]; then
    docker_run_args+=("${port_args[@]}")
  fi
  docker_run_args+=(-w "$REPO_ROOT" "$DIND_IMAGE")
  if [[ ${#daemon_args[@]} -gt 0 ]]; then
    docker_run_args+=("${daemon_args[@]}")
  fi
  docker "${docker_run_args[@]}"
else
  existing_runner_image="$(docker inspect -f '{{ index .Config.Labels "agent-fleet.runner-image" }}' "$DIND_NAME" 2>/dev/null || true)"
  if [[ "$existing_runner_image" != "$DIND_IMAGE" ]]; then
    err "existing DinD container $DIND_NAME uses a different runner image"
    err "existing: ${existing_runner_image:-<unlabeled>}"
    err "current:  $DIND_IMAGE"
    err "rerun with DIND_RECREATE=1 to recreate the DinD container while preserving Docker storage"
    exit 1
  fi
  existing_mirrors="$(docker inspect -f '{{ index .Config.Labels "agent-fleet.registry-mirrors" }}' "$DIND_NAME" 2>/dev/null || true)"
  if [[ "$existing_mirrors" != "$registry_mirrors" ]]; then
    err "existing DinD container $DIND_NAME was created with different registry mirrors"
    err "existing: ${existing_mirrors:-<empty>}"
    err "current:  ${registry_mirrors:-<empty>}"
    err "rerun with DIND_RECREATE=1 to recreate the DinD daemon while preserving Docker storage"
    exit 1
  fi
  existing_address_pools="$(docker inspect -f '{{ index .Config.Labels "agent-fleet.default-address-pools" }}' "$DIND_NAME" 2>/dev/null || true)"
  if [[ "$existing_address_pools" != "$default_address_pools" ]]; then
    err "existing DinD container $DIND_NAME was created with different default address pools"
    err "existing: ${existing_address_pools:-<empty>}"
    err "current:  ${default_address_pools:-<empty>}"
    err "rerun with DIND_RECREATE=1 to recreate the DinD daemon while preserving Docker storage"
    exit 1
  fi
  if ! container_running; then
    info "starting existing DinD container: $DIND_NAME"
    docker start "$DIND_NAME" >/dev/null
  else
    info "using running DinD container: $DIND_NAME"
  fi
fi

info "waiting for DinD daemon"
ready=0
for _ in $(seq 1 "$DIND_DAEMON_READY_TIMEOUT"); do
  if docker exec "$DIND_NAME" docker info >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  err "DinD daemon did not become ready in ${DIND_DAEMON_READY_TIMEOUT}s"
  exit 1
fi

if ! docker exec "$DIND_NAME" id "$DIND_USER" >/dev/null 2>&1; then
  err "DinD user does not exist: $DIND_USER"
  err "Set DIND_USER to a user provided by $DIND_IMAGE, or rebuild the default image."
  exit 1
fi
if [[ "$DIND_USER" != "root" ]]; then
  current_uid="$(docker exec "$DIND_NAME" id -u "$DIND_USER")"
  current_gid="$(docker exec "$DIND_NAME" id -g "$DIND_USER")"
  if [[ "$current_gid" != "$DIND_USER_GID" ]]; then
    docker exec "$DIND_NAME" groupmod -o -g "$DIND_USER_GID" "$DIND_USER"
  fi
  if [[ "$current_uid" != "$DIND_USER_UID" || "$current_gid" != "$DIND_USER_GID" ]]; then
    docker exec "$DIND_NAME" usermod -o -u "$DIND_USER_UID" -g "$DIND_USER_GID" "$DIND_USER"
  fi
  docker exec "$DIND_NAME" chown -R "$DIND_USER" "$DIND_HOME_DIR"
fi

declare -a exec_base=(exec --user "$DIND_USER")
if [[ "${DIND_TTY:-auto}" == "1" || ( "${DIND_TTY:-auto}" == "auto" && -t 0 && -t 1 ) ]]; then
  exec_base+=(-it)
fi

docker_exec() {
  docker "${exec_base[@]}" "$DIND_NAME" "$@"
}

declare -a run_env=(
  "REPO_DIR=$REPO_ROOT"
  "BASE_URL=$BASE_URL"
  "API_KEY=$API_KEY"
  "MODEL=$MODEL"
  "HOME=$DIND_HOME_DIR"
)
run_env+=("${proxy_env[@]}")
for optional in PI_VERSION TRACE_TO_OPIK OPIK_URL OPIK_API_KEY OPIK_WORKSPACE OPIK_PROJECT_NAME CLAUDE_TGZ_SOURCE CLAUDE_WHEEL_DIR_SOURCE TB_CC_CLAUDE_TGZ_SOURCE TB_CC_PY_WHEEL_DIR_SOURCE; do
  if [[ -n "${!optional:-}" ]]; then
    run_env+=("$optional=${!optional}")
  fi
done
for optional in PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST NPM_CONFIG_REGISTRY GO111MODULE GOPROXY GOSUMDB RUSTUP_UPDATE_ROOT RUSTUP_DIST_SERVER CARGO_REGISTRY_REPLACE_WITH CARGO_REGISTRY_URL DIND_REGISTRY_MIRRORS DIND_REGISTRY_MIRROR TB_SKIP_DOCKERHUB_PREFLIGHT; do
  if [[ -n "${!optional:-}" ]]; then
    run_env+=("$optional=${!optional}")
  fi
done
if [[ -n "$registry_mirrors" && -z "${TB_SKIP_DOCKERHUB_PREFLIGHT:-}" ]]; then
  run_env+=("TB_SKIP_DOCKERHUB_PREFLIGHT=1")
fi

docker_exec_env() {
  docker_exec env "${run_env[@]}" "$@"
}

docker_exec_root_env() {
  docker exec "$DIND_NAME" env "${run_env[@]}" "$@"
}

run_setup=0
case "$DIND_BOOTSTRAP" in
  always)
    run_setup=1
    ;;
  missing)
    # Extract the version number instead of matching the whole output line:
    # setup.sh does the same, and an exact-line match would re-run setup on
    # every invocation if `pi --version` ever prints more than the bare version.
    if ! docker_exec sh -c 'command -v pi >/dev/null 2>&1 && [ "$(pi --version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -n 1)" = "$3" ] && test -f "$1" && test -d "$2"' \
      sh "$DIND_HOME_DIR/.pi/agent/models.json" \
      "$DIND_HOME_DIR/.pi/agent/skills/harbor-benchmark-runner" \
      "${PI_VERSION:-0.81.1}"; then
      run_setup=1
    fi
    ;;
  skip)
    run_setup=0
    ;;
esac

if [[ "$run_setup" == "1" ]]; then
  info "running scripts/setup.sh inside DinD"
  docker_exec_root_env ./scripts/setup.sh
  if [[ "$DIND_USER" != "root" ]]; then
    docker exec "$DIND_NAME" chown -R "$DIND_USER" "$DIND_HOME_DIR"
  fi
fi

info "running scripts/run_fleet.sh ${fleet_args[*]} inside DinD"
docker_exec_env ./scripts/run_fleet.sh "${fleet_args[@]}"
