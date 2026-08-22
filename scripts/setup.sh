#!/usr/bin/env bash
# ============================================================
# setup.sh - Agent Fleet one-shot environment setup
#
# Idempotent: safe to re-run on failure; existing values are
# merged, not overwritten.
# ============================================================

set -euo pipefail

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()   { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=prerequisites.sh
source "$SCRIPT_DIR/prerequisites.sh"
if [[ "$AGENT_FLEET_BIN_DIR" == "$HOME/.local/bin" ]]; then
  AGENT_FLEET_BIN_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/agent-fleet/bin"
  warn "Migrating managed executables to project-private path: $AGENT_FLEET_BIN_DIR"
fi

# ---- Hardcoded versions (override via env if needed) ----
NODE_VERSION="${NODE_VERSION:-24}"
PI_VERSION="${PI_VERSION:-0.81.1}"
REPO_URL="${REPO_URL:-https://github.com/sii-system/agent-fleet.git}"
REPO_DIR="${REPO_DIR:-$SOURCE_REPO_ROOT}"
CONFIG_LOCAL="$REPO_DIR/config.local.env"

trim_setup_config_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_existing_setup_config() {
  local line key value first last
  [[ -f "$CONFIG_LOCAL" ]] || return 0
  if [[ ! -r "$CONFIG_LOCAL" ]]; then
    err "Existing config is not readable: $CONFIG_LOCAL"
    return 1
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(trim_setup_config_value "$line")"
    [[ -n "$line" && "${line:0:1}" != "#" && "$line" == *"="* ]] || continue
    if [[ "$line" == export[[:space:]]* ]]; then
      line="$(trim_setup_config_value "${line#export}")"
    fi
    key="$(trim_setup_config_value "${line%%=*}")"
    value="$(trim_setup_config_value "${line#*=}")"
    case "$key" in
      BASE_URL|API_KEY|AUTH_TOKEN|MODEL|\
      OPIK_URL|OPIK_API_KEY|OPIK_WORKSPACE|OPIK_PROJECT_NAME|\
      CLAUDE_TGZ_SOURCE|CLAUDE_WHEEL_DIR_SOURCE|\
      HARBOR_CC_CLAUDE_TGZ_SOURCE|HARBOR_CC_PY_WHEEL_DIR_SOURCE|\
      RL_ENVIRONMENT_TYPE|HARBOR_ENVIRONMENT_TYPE|AGENT_FLEET_REQUIRE_DOCKER)
        ;;
      *)
        continue
        ;;
    esac

    if [[ "$key" == "OPIK_URL" ]]; then
      SETUP_SAVED_HAS_OPIK_URL=1
    fi

    # setup writes plain values, but accept a simple matching quote pair from
    # hand-written files too. Do not eval the credential-bearing config.
    if (( ${#value} >= 2 )); then
      first="${value:0:1}"
      last="${value: -1}"
      if [[ "$first" == "$last" && ( "$first" == "'" || "$first" == '"' ) ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    # An explicitly supplied caller value, including an empty value, wins.
    if ! declare -p "$key" >/dev/null 2>&1; then
      printf -v "$key" '%s' "$value"
    fi
  done < "$CONFIG_LOCAL"
}

# Load the saved backend before prerequisite validation: qz/e2b runner hosts
# intentionally do not need Docker. Capture caller intent first so values read
# from config.local.env are not mistaken for explicit inputs to this setup run.
SETUP_CALLER_HAS_OPIK_URL=0
SETUP_SAVED_HAS_OPIK_URL=0
if declare -p OPIK_URL >/dev/null 2>&1; then
  SETUP_CALLER_HAS_OPIK_URL=1
fi
load_existing_setup_config

# ---- 1. Validate system prerequisites and install managed tools ----
info "Checking runtime prerequisites..."
if ! agent_fleet_bootstrap_setup_prerequisites; then
  err "Prerequisite setup failed."
  exit 1
fi
ok "Runtime prerequisites ready"
info "Managed executables: $AGENT_FLEET_BIN_DIR"
info "Prerequisite downloads: $AGENT_FLEET_CACHE_DIR/downloads"

# ---- 2. Gather config (caller env, existing local config, then prompts) ----

# OPIK_URL is the single switch: an endpoint uploads traces, empty disables
# tracing everywhere. Only prompt when neither the caller nor the saved config
# supplied a value, so a re-run does not re-ask.
configure_opik() {
  if (( SETUP_CALLER_HAS_OPIK_URL )); then
    OPIK_URL="$(trim_setup_config_value "${OPIK_URL:-}")"
  fi

  if [[ -z "${OPIK_URL:-}" ]] &&
     (( ! SETUP_CALLER_HAS_OPIK_URL && ! SETUP_SAVED_HAS_OPIK_URL )); then
    if ! read -rp "OPIK_URL (optional; press Enter to disable Opik): " OPIK_URL; then
      echo
      OPIK_URL=""
    fi
  fi
  OPIK_URL="$(trim_setup_config_value "${OPIK_URL:-}")"

  if [[ -n "${OPIK_URL:-}" ]]; then
    OPIK_WORKSPACE="${OPIK_WORKSPACE:-default}"
    OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-agent-fleet}"
    ok "Opik enabled"
  else
    ok "Opik disabled"
  fi
}

# Credentials came from the caller environment first, then the existing
# checkout config. Prompt only for values that are still missing.
info "Gathering model endpoint config..."
[[ -z "${BASE_URL:-}" ]]   && read -rp "BASE_URL (model gateway, WITHOUT /v1): " BASE_URL
# Accept API_KEY as the repo-standard alias for AUTH_TOKEN
AUTH_TOKEN="${AUTH_TOKEN:-${API_KEY:-}}"
if [[ -z "${AUTH_TOKEN:-}" ]]; then
  read -rsp "AUTH_TOKEN (or API_KEY, input hidden): " AUTH_TOKEN
  echo
fi
[[ -z "${MODEL:-}" ]]      && read -rp "MODEL (model id): " MODEL
configure_opik

for v in BASE_URL AUTH_TOKEN MODEL; do
  if [[ -z "${!v:-}" ]]; then
    err "Config '$v' is empty, aborting."
    exit 1
  fi
done
ok "Config gathered (BASE_URL=${BASE_URL}, MODEL=${MODEL})"

# Validate optional local Claude package config (for benchmark containers).
# Use the repo-standard HARBOR_CC_* names; accept the short aliases too.
load_legacy_managed_claude_package_config() {
  local bashrc="$HOME/.bashrc"
  local key value found=0
  [[ -r "$bashrc" ]] || return 0

  while IFS=$'\t' read -r key value; do
    found=1
    case "$key" in
      HARBOR_CC_OPIK_ENABLE_HOOK)
        if ! declare -p HARBOR_CC_OPIK_ENABLE_HOOK >/dev/null 2>&1; then
          printf -v HARBOR_CC_OPIK_ENABLE_HOOK '%s' "$value"
        fi
        ;;
      HARBOR_CC_CLAUDE_TGZ_SOURCE)
        if ! declare -p CLAUDE_TGZ_SOURCE >/dev/null 2>&1 &&
           ! declare -p HARBOR_CC_CLAUDE_TGZ_SOURCE >/dev/null 2>&1; then
          printf -v HARBOR_CC_CLAUDE_TGZ_SOURCE '%s' "$value"
        fi
        ;;
      HARBOR_CC_PY_WHEEL_DIR_SOURCE)
        if ! declare -p CLAUDE_WHEEL_DIR_SOURCE >/dev/null 2>&1 &&
           ! declare -p HARBOR_CC_PY_WHEEL_DIR_SOURCE >/dev/null 2>&1; then
          printf -v HARBOR_CC_PY_WHEEL_DIR_SOURCE '%s' "$value"
        fi
        ;;
    esac
  done < <(python3 "$SCRIPT_DIR/setup_config.py" legacy-claude-config "$bashrc")

  if (( found )); then
    warn "Migrating legacy TerminalBench Claude package settings from ~/.bashrc."
  fi
}

load_legacy_managed_claude_package_config
CLAUDE_TGZ_SOURCE="${CLAUDE_TGZ_SOURCE:-${HARBOR_CC_CLAUDE_TGZ_SOURCE:-}}"
CLAUDE_WHEEL_DIR_SOURCE="${CLAUDE_WHEEL_DIR_SOURCE:-${HARBOR_CC_PY_WHEEL_DIR_SOURCE:-}}"
if [[ -n "${CLAUDE_TGZ_SOURCE:-}" || -n "${CLAUDE_WHEEL_DIR_SOURCE:-}" ]]; then
  if [[ -z "${CLAUDE_TGZ_SOURCE:-}" || -z "${CLAUDE_WHEEL_DIR_SOURCE:-}" ]]; then
    warn "Only one of CLAUDE_TGZ_SOURCE / CLAUDE_WHEEL_DIR_SOURCE is set; both are needed. Ignoring local package."
    CLAUDE_TGZ_SOURCE=""; CLAUDE_WHEEL_DIR_SOURCE=""
  elif [[ ! -f "${CLAUDE_TGZ_SOURCE}" ]]; then
    warn "CLAUDE_TGZ_SOURCE not found: ${CLAUDE_TGZ_SOURCE} -- containers will fall back to public installer. Ignoring."
    CLAUDE_TGZ_SOURCE=""; CLAUDE_WHEEL_DIR_SOURCE=""
  elif [[ ! -d "${CLAUDE_WHEEL_DIR_SOURCE}/npm-cache" ]]; then
    warn "CLAUDE_WHEEL_DIR_SOURCE has no npm-cache/ subdir: ${CLAUDE_WHEEL_DIR_SOURCE} -- ignoring local package."
    CLAUDE_TGZ_SOURCE=""; CLAUDE_WHEEL_DIR_SOURCE=""
  else
    ok "Local Claude package configured for containers: ${CLAUDE_TGZ_SOURCE}"
  fi
fi

# ---- 3. Ensure Node >=22.19 (via nvm if needed) ----
node_version_ok() {
  local version major minor
  version="${1#v}"
  IFS=. read -r major minor _ <<<"$version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  (( major > 22 || (major == 22 && minor >= 19) ))
}
load_nvm() {
  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
  # Sourcing is best-effort: a missing nvm.sh (or a non-zero return from it)
  # must not abort the script under `set -e`. Callers gate on `command -v
  # node`/`nvm` afterwards, so a missing nvm here is handled, not fatal.
  return 0
}

load_nvm
NEED_NODE=1
if command -v node >/dev/null 2>&1; then
  CUR_NODE_VERSION="$(node -v 2>/dev/null || true)"
  if node_version_ok "$CUR_NODE_VERSION"; then
    ok "Node $CUR_NODE_VERSION OK (>=22.19)"
    NEED_NODE=0
  else
    warn "Node $(node -v) too old, will install Node $NODE_VERSION via nvm"
  fi
else
  warn "Node not found, will install Node $NODE_VERSION via nvm"
fi

if [[ "$NEED_NODE" == "1" ]]; then
  if ! command -v nvm >/dev/null 2>&1; then
    info "Installing nvm..."
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
    load_nvm
  fi
  if ! command -v nvm >/dev/null 2>&1; then
    err "nvm install/load failed. Please install Node >=22.19 manually and re-run."
    exit 1
  fi
  info "Installing Node $NODE_VERSION via nvm..."
  nvm install "$NODE_VERSION"
  nvm use "$NODE_VERSION"
  nvm alias default "$NODE_VERSION" || true
  CUR_NODE_VERSION="$(node -v 2>/dev/null || true)"
  if node_version_ok "$CUR_NODE_VERSION"; then
    ok "Node $CUR_NODE_VERSION ready"
  else
    err "Node still <22.19 after install, aborting."
    exit 1
  fi
fi

if ! command -v npm >/dev/null 2>&1; then
  err "npm not found even after Node setup, aborting."
  exit 1
fi

# Keep both the selected Node runtime and the managed Pi installation
# discoverable without sourcing an interactive shell startup file.
AGENT_FLEET_NODE_BIN_DIR="$(dirname "$(command -v node)")"
AGENT_FLEET_NPM_PREFIX="${AGENT_FLEET_NPM_PREFIX:-$AGENT_FLEET_CACHE_DIR/npm}"
AGENT_FLEET_NPM_BIN_DIR="$AGENT_FLEET_NPM_PREFIX/bin"
agent_fleet_prerequisite_init_path

# ---- 4. Install Pi for control-plane use ----
info "Checking Pi version..."
NEED_INSTALL=1
if [[ -x "$AGENT_FLEET_NPM_BIN_DIR/pi" ]]; then
  CUR_VER="$("$AGENT_FLEET_NPM_BIN_DIR/pi" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ "$CUR_VER" == "$PI_VERSION" ]]; then
    ok "Pi already at target version $PI_VERSION"
    NEED_INSTALL=0
  else
    warn "Current Pi version ${CUR_VER:-unknown}, switching to $PI_VERSION"
  fi
else
  warn "Pi not found, installing $PI_VERSION"
fi
if [[ "$NEED_INSTALL" == "1" ]]; then
  info "Installing Pi @${PI_VERSION}..."
  npm install -g --prefix "$AGENT_FLEET_NPM_PREFIX" --ignore-scripts \
    "@earendil-works/pi-coding-agent@${PI_VERSION}" --force
  hash -r
  CUR_VER="$("$AGENT_FLEET_NPM_BIN_DIR/pi" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ "$CUR_VER" != "$PI_VERSION" ]]; then
    err "Pi install did not provide target version $PI_VERSION (got ${CUR_VER:-unknown})"
    exit 1
  fi
  ok "Pi ${PI_VERSION} installed"
  info "Override pinned version via PI_VERSION only after verifying compatibility"
fi

# Persist the managed Pi executable directory so public runners can find it
# without relying on an interactive shell startup file.
agent_fleet_prerequisite_init_path
if [[ ! -x "$AGENT_FLEET_NPM_BIN_DIR/pi" ]]; then
  err "Pi was installed but is not executable from $AGENT_FLEET_NPM_BIN_DIR"
  exit 1
fi
agent_fleet_save_prerequisite_paths
ok "Pi executable: $AGENT_FLEET_NPM_BIN_DIR/pi"

# ---- 5. Merge the managed Pi provider and settings ----
info "Merging managed Pi configuration..."
PI_AGENT_DIR="$HOME/.pi/agent"
mkdir -p "$PI_AGENT_DIR"
PI_SETTINGS="$PI_AGENT_DIR/settings.json"
PI_MODELS="$PI_AGENT_DIR/models.json"
cp -f "$PI_SETTINGS" "$PI_SETTINGS.bak.agent-fleet" 2>/dev/null || true
cp -f "$PI_MODELS" "$PI_MODELS.bak.agent-fleet" 2>/dev/null || true
python3 "$SCRIPT_DIR/setup_config.py" merge-pi-config \
  "$PI_SETTINGS" "$PI_MODELS" "$BASE_URL" "$MODEL"
ok "Pi configuration merged (provider=sii-gateway, model=${MODEL})"

# ---- 6. Write env vars + nvm init to ~/.bashrc (idempotent) ----
info "Writing env vars to ~/.bashrc..."
BASHRC="$HOME/.bashrc"
cp -f "$BASHRC" "$BASHRC.bak.agent-fleet" 2>/dev/null || true
AUTH_TOKEN="$AUTH_TOKEN" \
CLAUDE_TGZ_SOURCE="$CLAUDE_TGZ_SOURCE" \
CLAUDE_WHEEL_DIR_SOURCE="$CLAUDE_WHEEL_DIR_SOURCE" \
AGENT_FLEET_PATHS_FILE="$AGENT_FLEET_PATHS_FILE" \
BASHRC="$BASHRC" \
  python3 "$SCRIPT_DIR/setup_config.py" update-bashrc "$BASHRC"
ok "Env vars written to ~/.bashrc (idempotent); backup at ${BASHRC}.bak.agent-fleet"

export PI_OFFLINE=1
export AGENT_FLEET_API_KEY="${AUTH_TOKEN}"
if [[ -n "${CLAUDE_TGZ_SOURCE:-}" && -n "${CLAUDE_WHEEL_DIR_SOURCE:-}" ]]; then
  export HARBOR_CC_OPIK_ENABLE_HOOK=1
  export HARBOR_CC_CLAUDE_TGZ_SOURCE="${CLAUDE_TGZ_SOURCE}"
  export HARBOR_CC_PY_WHEEL_DIR_SOURCE="${CLAUDE_WHEEL_DIR_SOURCE}"
fi

# ---- 7. Clone repo ----
repo_destination_has_entries() (
  local entries=()
  shopt -s dotglob nullglob
  entries=("$1"/*)
  (( ${#entries[@]} > 0 ))
)

git_probe_output=""
if git_probe_output="$(git -C "$REPO_DIR" rev-parse --git-dir 2>&1)"; then
  ok "Repo already exists: $REPO_DIR (skip clone)"
else
  git_probe_status=$?
  if [[ -e "$REPO_DIR/.git" || -L "$REPO_DIR/.git" ]]; then
    err "Cannot inspect existing repository at $REPO_DIR; refusing to clone over it."
  elif [[ -e "$REPO_DIR" || -L "$REPO_DIR" ]]; then
    if [[ ! -d "$REPO_DIR" ]]; then
      err "Repository destination exists and is not a directory: $REPO_DIR"
    elif [[ ! -r "$REPO_DIR" || ! -x "$REPO_DIR" ]]; then
      err "Cannot inspect repository destination: $REPO_DIR"
    elif repo_destination_has_entries "$REPO_DIR"; then
      err "Git repository probe failed and destination is not empty: $REPO_DIR"
    else
      info "Cloning repo to $REPO_DIR..."
      git clone --recurse-submodules "$REPO_URL" "$REPO_DIR"
      ok "Repo cloned"
      git_probe_status=0
    fi
  else
    info "Cloning repo to $REPO_DIR..."
    git clone --recurse-submodules "$REPO_URL" "$REPO_DIR"
    ok "Repo cloned"
    git_probe_status=0
  fi

  if [[ "$git_probe_status" != "0" ]]; then
    if [[ -n "$git_probe_output" ]]; then
      printf '%s\n' "$git_probe_output" >&2
    else
      err "git rev-parse failed with status $git_probe_status"
    fi
    exit "$git_probe_status"
  fi
fi
info "Syncing submodules..."
if ! git -C "$REPO_DIR" submodule sync --recursive ||
   ! git -C "$REPO_DIR" submodule update --init --recursive; then
  err "Submodule sync failed; the tracing plugin is required for Opik-enabled runs."
  exit 1
fi

info "Enabling repository Git hooks..."
"$SOURCE_REPO_ROOT/scripts/install-git-hooks.sh" "$REPO_DIR"
ok "Git hooks enabled"

if [[ ! -d "$REPO_DIR/skills" ]]; then
  err "$REPO_DIR/skills not found, repo structure looks wrong"
  exit 1
fi

# ---- 8. Prepare the pinned host Harbor runner ----
case "${HARBOR_RUNNER_SETUP:-1}" in
  1|true|yes)
    info "Preparing pinned Harbor runner environment..."
    "$SOURCE_REPO_ROOT/Agents/utils/common/Harbor/setup_runner_env.sh"
    ;;
  0|false|no)
    warn "Skipping Harbor runner setup because HARBOR_RUNNER_SETUP=${HARBOR_RUNNER_SETUP}"
    ;;
  *)
    err "HARBOR_RUNNER_SETUP must be 1 or 0"
    exit 1
    ;;
esac

# ---- 9. Install Pi skills ----
info "Installing Pi skills..."
PI_SKILLS_DIR="$PI_AGENT_DIR/skills"
mkdir -p "$PI_SKILLS_DIR"
SKILLS=(
  harbor-benchmark-runner
  openclaw-fleet-operations
  openclaw-benchmark-runners
)
for skill in "${SKILLS[@]}"; do
  if [[ -d "$REPO_DIR/skills/$skill" ]]; then
    ln -sfn "$REPO_DIR/skills/$skill" "$PI_SKILLS_DIR/$skill"
  else
    warn "skill dir not found: $REPO_DIR/skills/$skill"
  fi
done
ok "Pi skills installed to $PI_SKILLS_DIR"

# ---- 10. Merge managed keys into config.local.env ----
# Update only the keys setup.sh manages (BASE_URL/API_KEY/MODEL + tracing/Opik),
# preserve any other private overrides the user has added (mirrors, etc.).
# BASE_URL is stored as-is (without /v1), matching the repo convention:
# config.env documents BASE_URL as the API root without a version suffix;
# runners append /v1 themselves.
info "Merging managed keys into $CONFIG_LOCAL..."
CONFIG_LOCAL_BACKUP=""
if [[ -f "$CONFIG_LOCAL" ]]; then
  CONFIG_LOCAL_BACKUP="${CONFIG_LOCAL}.bak.agent-fleet"
  cp -f "$CONFIG_LOCAL" "$CONFIG_LOCAL_BACKUP"
  chmod 0600 "$CONFIG_LOCAL_BACKUP"
fi
BASE_URL="$BASE_URL" \
AUTH_TOKEN="$AUTH_TOKEN" \
MODEL="$MODEL" \
OPIK_URL="${OPIK_URL:-}" \
OPIK_API_KEY="${OPIK_API_KEY:-}" \
OPIK_WORKSPACE="${OPIK_WORKSPACE:-}" \
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-}" \
CONFIG_LOCAL="$CONFIG_LOCAL" \
  python3 "$SCRIPT_DIR/setup_config.py" merge-local-config "$CONFIG_LOCAL"
chmod 0600 "$CONFIG_LOCAL"
if [[ -n "$CONFIG_LOCAL_BACKUP" ]]; then
  ok "config.local.env merged; private backup at $CONFIG_LOCAL_BACKUP"
else
  ok "config.local.env created"
fi

# ---- 11. Docker permission check ----
if agent_fleet_docker_required; then
  info "Checking Docker permission..."
  if docker ps >/dev/null 2>&1; then
    ok "Docker permission OK"
  else
    err "Current user cannot access the Docker daemon."
    err "Add the user to the Docker group (or configure rootless/remote Docker), reopen the shell, and re-run setup."
    exit 1
  fi
else
  info "Skipping Docker permission check: the configured sandbox backend does not need it"
fi

echo
ok "========================================"
ok " Environment setup complete!"
ok "========================================"
echo
info "Idempotent: safe to re-run if something failed."
info "Ready to run now: ./scripts/run_fleet.sh --help"
info "Run outputs: $REPO_DIR/runs"
info "Repository runners load the saved paths automatically; no shell reload is required."
