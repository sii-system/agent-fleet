#!/usr/bin/env bash
# Shared prerequisite discovery and managed-tool bootstrap.
set -euo pipefail

AGENT_FLEET_PATHS_FILE="${AGENT_FLEET_PATHS_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/agent-fleet/paths.env}"
__agent_fleet_caller_env="$(export -p)"
if [[ -f "$AGENT_FLEET_PATHS_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$AGENT_FLEET_PATHS_FILE"
fi
eval "$__agent_fleet_caller_env"
unset __agent_fleet_caller_env

AGENT_FLEET_BIN_DIR="${AGENT_FLEET_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/agent-fleet/bin}"
AGENT_FLEET_CACHE_DIR="${AGENT_FLEET_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/agent-fleet}"
AGENT_FLEET_NODE_BIN_DIR="${AGENT_FLEET_NODE_BIN_DIR:-}"
AGENT_FLEET_NPM_BIN_DIR="${AGENT_FLEET_NPM_BIN_DIR:-}"
AGENT_FLEET_RUNTIME_DIR="${AGENT_FLEET_RUNTIME_DIR:-/tmp/agent-fleet-z-$UID}"
ZELLIJ_VERSION="${ZELLIJ_VERSION:-0.44.3}"
UV_VERSION="${UV_VERSION:-0.11.28}"

agent_fleet_prereq_info() { printf '[INFO] %s\n' "$*"; }
agent_fleet_prereq_ok() { printf '[ OK ] %s\n' "$*"; }
agent_fleet_prereq_error() { printf '[FAIL] %s\n' "$*" >&2; }

agent_fleet_prerequisite_init_runtime() {
  if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" &&
        -w "$XDG_RUNTIME_DIR" && -x "$XDG_RUNTIME_DIR" &&
        -O "$XDG_RUNTIME_DIR" ]]; then
    return 0
  fi
  if [[ -L "$AGENT_FLEET_RUNTIME_DIR" ]]; then
    agent_fleet_prereq_error "runtime path must not be a symbolic link: $AGENT_FLEET_RUNTIME_DIR"
    return 1
  fi
  if [[ -e "$AGENT_FLEET_RUNTIME_DIR" &&
        ! -O "$AGENT_FLEET_RUNTIME_DIR" ]]; then
    agent_fleet_prereq_error "runtime path is not owned by the current user: $AGENT_FLEET_RUNTIME_DIR"
    return 1
  fi
  mkdir -p "$AGENT_FLEET_RUNTIME_DIR" ||
    { agent_fleet_prereq_error "cannot create runtime path: $AGENT_FLEET_RUNTIME_DIR"; return 1; }
  if [[ -L "$AGENT_FLEET_RUNTIME_DIR" ||
        ! -d "$AGENT_FLEET_RUNTIME_DIR" ||
        ! -O "$AGENT_FLEET_RUNTIME_DIR" ]]; then
    agent_fleet_prereq_error "runtime path is not a user-owned directory: $AGENT_FLEET_RUNTIME_DIR"
    return 1
  fi
  chmod 0700 "$AGENT_FLEET_RUNTIME_DIR" ||
    { agent_fleet_prereq_error "cannot secure runtime path: $AGENT_FLEET_RUNTIME_DIR"; return 1; }
  XDG_RUNTIME_DIR="$AGENT_FLEET_RUNTIME_DIR"
  export XDG_RUNTIME_DIR AGENT_FLEET_RUNTIME_DIR
}

agent_fleet_prepend_path() {
  local directory="$1" rest="${PATH-}" entry rebuilt=""
  local more=0 has_entry=0
  [[ -n "$directory" ]] || return 0
  while :; do
    if [[ "$rest" == *:* ]]; then
      entry="${rest%%:*}"
      rest="${rest#*:}"
      more=1
    else
      entry="$rest"
      more=0
    fi
    if [[ "$entry" != "$directory" ]]; then
      (( has_entry )) && rebuilt+=":"
      rebuilt+="$entry"
      has_entry=1
    fi
    (( more )) || break
  done
  if (( has_entry )); then
    PATH="$directory:$rebuilt"
  else
    PATH="$directory"
  fi
}

agent_fleet_prerequisite_init_path() {
  UV_TOOL_BIN_DIR="${UV_TOOL_BIN_DIR:-$AGENT_FLEET_BIN_DIR}"
  UV_CACHE_DIR="${UV_CACHE_DIR:-$AGENT_FLEET_CACHE_DIR/uv/cache}"
  agent_fleet_prepend_path "$AGENT_FLEET_BIN_DIR"
  agent_fleet_prepend_path "$AGENT_FLEET_NODE_BIN_DIR"
  agent_fleet_prepend_path "$AGENT_FLEET_NPM_BIN_DIR"
  export PATH UV_TOOL_BIN_DIR UV_CACHE_DIR
  export AGENT_FLEET_BIN_DIR AGENT_FLEET_CACHE_DIR
  export AGENT_FLEET_NODE_BIN_DIR AGENT_FLEET_NPM_BIN_DIR
  export AGENT_FLEET_PATHS_FILE
}

agent_fleet_save_prerequisite_paths() {
  local temporary="${AGENT_FLEET_PATHS_FILE}.tmp.$$"
  mkdir -p "$(dirname "$AGENT_FLEET_PATHS_FILE")" ||
    { agent_fleet_prereq_error "cannot create paths-file directory: $AGENT_FLEET_PATHS_FILE"; return 1; }
  {
    printf '# Managed by agent-fleet scripts/prerequisites.sh\n'
    printf 'export AGENT_FLEET_BIN_DIR=%q\n' "$AGENT_FLEET_BIN_DIR"
    printf 'export AGENT_FLEET_CACHE_DIR=%q\n' "$AGENT_FLEET_CACHE_DIR"
    printf 'export AGENT_FLEET_NODE_BIN_DIR=%q\n' "$AGENT_FLEET_NODE_BIN_DIR"
    printf 'export AGENT_FLEET_NPM_BIN_DIR=%q\n' "$AGENT_FLEET_NPM_BIN_DIR"
    printf '_agent_fleet_paths_prepend() {\n'
    printf '  local directory="$1" rest="${PATH-}" entry rebuilt=""\n'
    printf '  local more=0 has_entry=0\n'
    printf '  [ -n "$directory" ] || return 0\n'
    printf '  while :; do\n'
    printf '    if [[ "$rest" == *:* ]]; then entry="${rest%%%%:*}"; rest="${rest#*:}"; more=1\n'
    printf '    else entry="$rest"; more=0; fi\n'
    printf '    if [[ "$entry" != "$directory" ]]; then\n'
    printf '      (( has_entry )) && rebuilt+=":"\n'
    printf '      rebuilt+="$entry"; has_entry=1\n'
    printf '    fi\n'
    printf '    (( more )) || break\n'
    printf '  done\n'
    printf '  if (( has_entry )); then PATH="$directory:$rebuilt"; else PATH="$directory"; fi\n'
    printf '}\n'
    printf '_agent_fleet_paths_prepend "$AGENT_FLEET_BIN_DIR"\n'
    printf '_agent_fleet_paths_prepend "$AGENT_FLEET_NODE_BIN_DIR"\n'
    printf '_agent_fleet_paths_prepend "$AGENT_FLEET_NPM_BIN_DIR"\n'
    printf 'unset -f _agent_fleet_paths_prepend\n'
    printf 'export PATH\n'
  } >"$temporary" ||
    { agent_fleet_prereq_error "cannot write prerequisite paths: $temporary"; return 1; }
  chmod 0644 "$temporary" &&
    mv -f "$temporary" "$AGENT_FLEET_PATHS_FILE" || {
      rm -f "$temporary"
      agent_fleet_prereq_error "cannot publish prerequisite paths: $AGENT_FLEET_PATHS_FILE"
      return 1
    }
  agent_fleet_prereq_ok "saved prerequisite paths: $AGENT_FLEET_PATHS_FILE"
}

agent_fleet_python_version_ok() {
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'
}

agent_fleet_zellij_version_ok() {
  local binary="${1:-}" actual
  if [[ -z "$binary" ]]; then
    binary="$(command -v zellij 2>/dev/null || true)"
  fi
  [[ -n "$binary" && -x "$binary" ]] || return 1
  actual="$("$binary" --version 2>/dev/null || true)"
  [[ "${actual##* }" == "$ZELLIJ_VERSION" ]]
}

agent_fleet_uv_pair_ok() {
  local uv_binary="${1:-}" uvx_binary="${2:-}" actual
  if [[ -z "$uv_binary" ]]; then
    uv_binary="$(command -v uv 2>/dev/null || true)"
  fi
  if [[ -z "$uvx_binary" ]]; then
    uvx_binary="$(command -v uvx 2>/dev/null || true)"
  fi
  [[ -n "$uv_binary" && -x "$uv_binary" &&
     -n "$uvx_binary" && -x "$uvx_binary" ]] || return 1
  actual="$("$uv_binary" --version 2>/dev/null || true)"
  actual="${actual#uv }"
  [[ "${actual%% *}" == "$UV_VERSION" ]]
}

agent_fleet_platform_asset() {
  local tool="$1" os arch libc="gnu"
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) agent_fleet_prereq_error "unsupported $tool architecture: $arch"; return 1 ;;
  esac
  case "$os:$tool" in
    Linux:zellij) printf 'zellij-%s-unknown-linux-musl.tar.gz\n' "$arch" ;;
    Linux:uv)
      ldd --version 2>&1 | grep -qi musl && libc="musl"
      printf 'uv-%s-unknown-linux-%s.tar.gz\n' "$arch" "$libc"
      ;;
    Darwin:*) printf '%s-%s-apple-darwin.tar.gz\n' "$tool" "$arch" ;;
    *) agent_fleet_prereq_error "unsupported $tool operating system: $os"; return 1 ;;
  esac
}

agent_fleet_verify_sha256() {
  python3 - "$1" "$2" <<'PY'
import hashlib, pathlib, sys
source = pathlib.Path(sys.argv[1])
expected = pathlib.Path(sys.argv[2]).read_text().split()[0].lower()
actual = hashlib.sha256(source.read_bytes()).hexdigest()
raise SystemExit(0 if actual == expected else 1)
PY
}

agent_fleet_download() {
  local url="$1" destination="$2" temporary="${2}.tmp.$$"
  mkdir -p "$(dirname "$destination")"
  rm -f "$temporary"
  curl -fL --retry 3 --connect-timeout 15 -o "$temporary" "$url" || {
    rm -f "$temporary"
    return 1
  }
  mv -f "$temporary" "$destination"
}

agent_fleet_install_archive() {
  local tool="$1" version="$2" asset="$3" archive_url="$4" checksum_url="$5"
  local checksum_mode="$6"
  shift 6
  local archive checksum temp_dir name binary checksum_binary
  archive="$AGENT_FLEET_CACHE_DIR/downloads/$tool/$version/$asset"
  checksum="$archive.sha256"
  if [[ "$checksum_mode" != "archive" && "$checksum_mode" != "binary" ]]; then
    agent_fleet_prereq_error "unsupported checksum mode: $checksum_mode"
    return 1
  fi
  if [[ ! -f "$archive" || ! -f "$checksum" ]] ||
     { [[ "$checksum_mode" == "archive" ]] &&
       ! agent_fleet_verify_sha256 "$archive" "$checksum"; }; then
    rm -f "$archive" "$checksum"
    agent_fleet_prereq_info "downloading $archive_url"
    agent_fleet_download "$archive_url" "$archive" ||
      { agent_fleet_prereq_error "download failed: $archive_url"; return 1; }
    agent_fleet_download "$checksum_url" "$checksum" ||
      { agent_fleet_prereq_error "checksum download failed: $checksum_url"; return 1; }
    if [[ "$checksum_mode" == "archive" ]] &&
       ! agent_fleet_verify_sha256 "$archive" "$checksum"; then
      rm -f "$archive" "$checksum"
      agent_fleet_prereq_error "SHA-256 verification failed: $archive"
      return 1
    fi
  else
    agent_fleet_prereq_ok "using cached download: $archive"
  fi

  temp_dir="$(mktemp -d)" ||
    { agent_fleet_prereq_error "cannot create temporary install directory"; return 1; }
  tar -xzf "$archive" -C "$temp_dir" || {
    rm -rf "$temp_dir"
    agent_fleet_prereq_error "cannot extract prerequisite archive: $archive"
    return 1
  }
  if [[ "$checksum_mode" == "binary" ]]; then
    checksum_binary="$(find "$temp_dir" -type f -name "$1" -print -quit)"
    if [[ -z "$checksum_binary" ]] ||
       ! agent_fleet_verify_sha256 "$checksum_binary" "$checksum"; then
      rm -rf "$temp_dir"
      rm -f "$archive" "$checksum"
      agent_fleet_prereq_error "SHA-256 verification failed for $1 in $asset"
      return 1
    fi
  fi
  mkdir -p "$AGENT_FLEET_BIN_DIR" || {
    rm -rf "$temp_dir"
    agent_fleet_prereq_error "cannot create managed executable directory: $AGENT_FLEET_BIN_DIR"
    return 1
  }
  for name in "$@"; do
    binary="$(find "$temp_dir" -type f -name "$name" -print -quit)"
    if [[ -z "$binary" ]]; then
      rm -rf "$temp_dir"
      agent_fleet_prereq_error "$asset did not contain $name"
      return 1
    fi
    if ! cp "$binary" "$AGENT_FLEET_BIN_DIR/.${name}.tmp.$$" ||
       ! chmod 0755 "$AGENT_FLEET_BIN_DIR/.${name}.tmp.$$" ||
       ! mv -f "$AGENT_FLEET_BIN_DIR/.${name}.tmp.$$" "$AGENT_FLEET_BIN_DIR/$name"; then
      rm -f "$AGENT_FLEET_BIN_DIR/.${name}.tmp.$$"
      rm -rf "$temp_dir"
      agent_fleet_prereq_error "cannot install managed executable: $AGENT_FLEET_BIN_DIR/$name"
      return 1
    fi
  done
  rm -rf "$temp_dir"
  hash -r
}

agent_fleet_install_zellij() {
  local asset archive_url checksum_url
  if [[ "${AGENT_FLEET_PREREQUISITES_FORCE_MANAGED:-0}" != "1" ]] && agent_fleet_zellij_version_ok; then
    agent_fleet_prereq_ok "zellij $(zellij --version) ($(command -v zellij))"
    return 0
  fi
  asset="$(agent_fleet_platform_asset zellij)" || return 1
  archive_url="${ZELLIJ_DOWNLOAD_URL:-https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/${asset}}"
  checksum_url="${ZELLIJ_CHECKSUM_URL:-${archive_url%.tar.gz}.sha256sum}"
  # Zellij's .sha256sum hashes the extracted binary, not the tar.gz archive.
  agent_fleet_install_archive zellij "$ZELLIJ_VERSION" "$asset" \
    "$archive_url" "$checksum_url" binary zellij || return 1
  agent_fleet_zellij_version_ok "$AGENT_FLEET_BIN_DIR/zellij" ||
    { agent_fleet_prereq_error "installed zellij is not the required version"; return 1; }
  agent_fleet_prereq_ok "installed $("$AGENT_FLEET_BIN_DIR/zellij" --version) to $AGENT_FLEET_BIN_DIR/zellij"
}

agent_fleet_install_uv() {
  local asset archive_url checksum_url
  if [[ "${AGENT_FLEET_PREREQUISITES_FORCE_MANAGED:-0}" != "1" ]] && agent_fleet_uv_pair_ok; then
    agent_fleet_prereq_ok "$(uv --version) ($(command -v uv)); uvx=$(command -v uvx)"
    return 0
  fi
  asset="$(agent_fleet_platform_asset uv)" || return 1
  archive_url="${UV_DOWNLOAD_URL:-https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${asset}}"
  checksum_url="${UV_CHECKSUM_URL:-${archive_url}.sha256}"
  agent_fleet_install_archive uv "$UV_VERSION" "$asset" \
    "$archive_url" "$checksum_url" archive uv uvx || return 1
  agent_fleet_uv_pair_ok "$AGENT_FLEET_BIN_DIR/uv" "$AGENT_FLEET_BIN_DIR/uvx" ||
    { agent_fleet_prereq_error "installed uv/uvx are not the required version"; return 1; }
  agent_fleet_prereq_ok "installed $("$AGENT_FLEET_BIN_DIR/uv" --version) and uvx to $AGENT_FLEET_BIN_DIR"
}

agent_fleet_check_commands() {
  local label="$1" command_name resolved failed=0
  shift
  for command_name in "$@"; do
    resolved="$(command -v "$command_name" 2>/dev/null || true)"
    if [[ -n "$resolved" ]]; then
      agent_fleet_prereq_ok "$command_name: $resolved"
    else
      agent_fleet_prereq_error "$label command is not on PATH: $command_name"
      failed=1
    fi
  done
  return "$failed"
}

agent_fleet_docker_required() {
  # qz and e2b run every task off-host and build nothing locally (qz templates
  # are platform-registered, e2b builds on the remote service), so their
  # runner hosts (for example SII notebooks) may not ship a docker client at
  # all. Docker stays required everywhere else, including opensandbox, whose
  # task images are built and pushed by the runner's local daemon.
  # AGENT_FLEET_REQUIRE_DOCKER forces either behavior explicitly (for example
  # 0 for opensandbox runs that only use a prebuilt
  # HARBOR_OPENSANDBOX_IMAGE_REF).
  case "${AGENT_FLEET_REQUIRE_DOCKER:-}" in
    0|false|no) return 1 ;;
    1|true|yes) return 0 ;;
  esac
  # HARBOR_ENVIRONMENT_TYPE is the effective per-run override used by env.sh;
  # RL_ENVIRONMENT_TYPE is its fallback.
  case "${HARBOR_ENVIRONMENT_TYPE:-${RL_ENVIRONMENT_TYPE:-docker}}" in
    qz|e2b) return 1 ;;
  esac
  return 0
}

agent_fleet_check_core() {
  local failed=0 docker_required=0
  agent_fleet_check_commands "required" \
    bash git curl jq python3 openssl awk sed grep find tar date mktemp nohup env \
    chmod cp dirname mkdir mv rm uname \
    || failed=1
  if agent_fleet_docker_required; then
    docker_required=1
    agent_fleet_check_commands "required" docker || failed=1
  elif ! command -v docker >/dev/null 2>&1; then
    agent_fleet_prereq_info "docker not found; continuing because the configured sandbox backend does not need it"
  fi
  if command -v python3 >/dev/null 2>&1 && ! agent_fleet_python_version_ok; then
    agent_fleet_prereq_error "python3 must be >=3.9: $(python3 --version 2>&1)"
    failed=1
  fi
  if (( docker_required )) && command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      agent_fleet_prereq_ok "docker compose: $(docker compose version --short 2>/dev/null || docker compose version)"
    else
      agent_fleet_prereq_error "Docker Compose v2 is required"
      failed=1
    fi
  fi
  return "$failed"
}

agent_fleet_check_harbor() {
  local failed=0
  agent_fleet_check_commands "Harbor" zellij uv uvx flock setsid script pkill stty seq ||
    failed=1
  if command -v zellij >/dev/null 2>&1 && ! agent_fleet_zellij_version_ok; then
    agent_fleet_prereq_error "zellij $ZELLIJ_VERSION is required"
    failed=1
  fi
  if command -v uv >/dev/null 2>&1 && ! agent_fleet_uv_pair_ok; then
    agent_fleet_prereq_error "uv and uvx $UV_VERSION are required"
    failed=1
  fi
  if command -v script >/dev/null 2>&1 &&
     ! script -q -c true /dev/null >/dev/null 2>&1; then
    agent_fleet_prereq_error "Harbor requires util-linux 'script' with -c support"
    failed=1
  fi
  return "$failed"
}

agent_fleet_bootstrap_setup_prerequisites() {
  agent_fleet_prerequisite_init_path
  agent_fleet_prerequisite_init_runtime
  agent_fleet_check_core || return 1
  if [[ "${AGENT_FLEET_PREREQUISITES_INSTALL_MANAGED:-1}" == "1" ]]; then
    # Setup must leave behind paths it owns and can reload later. A compatible
    # command found only through the caller's temporary PATH is not enough.
    AGENT_FLEET_PREREQUISITES_FORCE_MANAGED="${AGENT_FLEET_PREREQUISITES_FORCE_MANAGED:-1}" \
      agent_fleet_install_zellij || return 1
    AGENT_FLEET_PREREQUISITES_FORCE_MANAGED="${AGENT_FLEET_PREREQUISITES_FORCE_MANAGED:-1}" \
      agent_fleet_install_uv || return 1
  fi
  agent_fleet_check_harbor || return 1
  agent_fleet_save_prerequisite_paths
}
