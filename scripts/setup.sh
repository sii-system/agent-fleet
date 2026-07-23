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

# ---- Hardcoded versions (override via env if needed) ----
NODE_VERSION="${NODE_VERSION:-24}"
PI_VERSION="${PI_VERSION:-0.81.1}"
REPO_URL="${REPO_URL:-https://github.com/sii-system/agent-fleet.git}"
REPO_DIR="${REPO_DIR:-$HOME/agent-fleet}"

# ---- 1. Gather config (env vars first, then interactive prompt) ----
# Credentials: BASE_URL / MODEL come from env vars, or are prompted
# interactively if missing. AUTH_TOKEN accepts the repo-standard API_KEY
# variable as an alias (config.env uses API_KEY, not AUTH_TOKEN).
info "Gathering model endpoint config..."
[[ -z "${BASE_URL:-}" ]]   && read -rp "BASE_URL (model gateway, WITHOUT /v1): " BASE_URL
# Accept API_KEY as the repo-standard alias for AUTH_TOKEN
AUTH_TOKEN="${AUTH_TOKEN:-${API_KEY:-}}"
if [[ -z "${AUTH_TOKEN:-}" ]]; then
  read -rsp "AUTH_TOKEN (or API_KEY, input hidden): " AUTH_TOKEN
  echo
fi
[[ -z "${MODEL:-}" ]]      && read -rp "MODEL (model id, e.g. glm-5.1-fp8): " MODEL

for v in BASE_URL AUTH_TOKEN MODEL; do
  if [[ -z "${!v:-}" ]]; then
    err "Config '$v' is empty, aborting."
    exit 1
  fi
done
ok "Config gathered (BASE_URL=${BASE_URL}, MODEL=${MODEL})"

# Validate optional local Claude package config (for benchmark containers).
# Use the repo-standard TB_CC_* names; accept the short aliases too.
CLAUDE_TGZ_SOURCE="${CLAUDE_TGZ_SOURCE:-${TB_CC_CLAUDE_TGZ_SOURCE:-}}"
CLAUDE_WHEEL_DIR_SOURCE="${CLAUDE_WHEEL_DIR_SOURCE:-${TB_CC_PY_WHEEL_DIR_SOURCE:-}}"
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

# ---- 2. Base dependency check ----
info "Checking base dependencies..."
MISSING=()
for cmd in git curl jq docker python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    MISSING+=("$cmd")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  err "Missing dependencies: ${MISSING[*]}"
  err "Please install them first. e.g. Ubuntu: sudo apt install ${MISSING[*]}"
  exit 1
fi
ok "Base dependencies present (git / curl / jq / docker / python3)"

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

# ---- 4. Install Pi for control-plane use ----
info "Checking Pi version..."
NEED_INSTALL=1
if command -v pi >/dev/null 2>&1; then
  CUR_VER="$(pi --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
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
  npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" --force
  hash -r
  CUR_VER="$(pi --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ "$CUR_VER" != "$PI_VERSION" ]]; then
    err "Pi install did not provide target version $PI_VERSION (got ${CUR_VER:-unknown})"
    exit 1
  fi
  ok "Pi ${PI_VERSION} installed"
  info "Override pinned version via PI_VERSION only after verifying compatibility"
fi

# ---- 5. Merge the managed Pi provider and settings ----
info "Merging managed Pi configuration..."
PI_AGENT_DIR="$HOME/.pi/agent"
mkdir -p "$PI_AGENT_DIR"
PI_SETTINGS="$PI_AGENT_DIR/settings.json"
PI_MODELS="$PI_AGENT_DIR/models.json"
cp -f "$PI_SETTINGS" "$PI_SETTINGS.bak.agent-fleet" 2>/dev/null || true
cp -f "$PI_MODELS" "$PI_MODELS.bak.agent-fleet" 2>/dev/null || true
python3 - "$PI_SETTINGS" "$PI_MODELS" "$BASE_URL" "$MODEL" "$SCRIPT_DIR" <<'PY'
import json, sys
settings_path, models_path, base_url, model, script_dir = sys.argv[1:]
sys.path.insert(0, script_dir)
from pi_prompt import PromptFailure, models_config, normalized_base_url

def load_object(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        print(
            f"\033[1;33m[WARN]\033[0m existing {path} could not be parsed; "
            f"backed up at {path}.bak.agent-fleet, writing fresh",
            file=sys.stderr,
        )
    return {}

settings = load_object(settings_path)
settings["defaultProvider"] = "sii-gateway"
settings["defaultModel"] = model
settings.setdefault("defaultThinkingLevel", "high")
settings.setdefault("theme", "dark")
settings.setdefault("enableInstallTelemetry", False)

models = load_object(models_path)
providers = models.get("providers")
if not isinstance(providers, dict):
    providers = {}
    models["providers"] = providers
try:
    normalized_url = normalized_base_url(base_url)
except PromptFailure as exc:
    print(f"\033[1;31m[FAIL]\033[0m {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
providers["sii-gateway"] = models_config(
    normalized_url, model, display_name="Agent Fleet"
)["providers"]["sii-gateway"]

for path, value in ((settings_path, settings), (models_path, models)):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
PY
ok "Pi configuration merged (provider=sii-gateway, model=${MODEL})"

# ---- 6. Write env vars + nvm init to ~/.bashrc (idempotent) ----
# Uses Python to replace the managed block portably (GNU/BSD sed differ).
info "Writing env vars to ~/.bashrc..."
BASHRC="$HOME/.bashrc"
cp -f "$BASHRC" "$BASHRC.bak.agent-fleet" 2>/dev/null || true
AUTH_TOKEN="$AUTH_TOKEN" \
CLAUDE_TGZ_SOURCE="$CLAUDE_TGZ_SOURCE" \
CLAUDE_WHEEL_DIR_SOURCE="$CLAUDE_WHEEL_DIR_SOURCE" \
BASHRC="$BASHRC" \
  python3 - <<'PY'
import os, shlex
from pathlib import Path

bashrc = Path(os.environ["BASHRC"])
auth_token = os.environ["AUTH_TOKEN"]
tgz = os.environ.get("CLAUDE_TGZ_SOURCE", "").strip()
wheel = os.environ.get("CLAUDE_WHEEL_DIR_SOURCE", "").strip()

BEGIN = "# >>> agent-fleet env >>>"
END   = "# <<< agent-fleet env <<<"

lines = []
if bashrc.exists():
    lines = bashrc.read_text(encoding="utf-8").splitlines()

# Drop any existing managed block (idempotent).
out = []
in_block = False
for ln in lines:
    if ln.strip() == BEGIN:
        in_block = True
        continue
    if ln.strip() == END:
        in_block = False
        continue
    if not in_block:
        out.append(ln)

# Build new managed block with shell-escaped values.
q = shlex.quote
block = [
    "",
    BEGIN,
    'export NVM_DIR="$HOME/.nvm"',
    '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"',
    "export PI_OFFLINE=1",
    f"export AGENT_FLEET_API_KEY={q(auth_token)}",
]
if tgz and wheel:
    block += [
        "export TB_CC_OPIK_ENABLE_HOOK=1",
        f"export TB_CC_CLAUDE_TGZ_SOURCE={q(tgz)}",
        f"export TB_CC_PY_WHEEL_DIR_SOURCE={q(wheel)}",
    ]
block.append(END)

out.extend(block)
bashrc.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
ok "Env vars written to ~/.bashrc (idempotent); backup at ${BASHRC}.bak.agent-fleet"

export PI_OFFLINE=1
export AGENT_FLEET_API_KEY="${AUTH_TOKEN}"
if [[ -n "${CLAUDE_TGZ_SOURCE:-}" && -n "${CLAUDE_WHEEL_DIR_SOURCE:-}" ]]; then
  export TB_CC_OPIK_ENABLE_HOOK=1
  export TB_CC_CLAUDE_TGZ_SOURCE="${CLAUDE_TGZ_SOURCE}"
  export TB_CC_PY_WHEEL_DIR_SOURCE="${CLAUDE_WHEEL_DIR_SOURCE}"
fi

# ---- 7. Clone repo ----
if [[ -d "$REPO_DIR/.git" ]]; then
  ok "Repo already exists: $REPO_DIR (skip clone)"
else
  info "Cloning repo to $REPO_DIR..."
  git clone --recurse-submodules "$REPO_URL" "$REPO_DIR"
  ok "Repo cloned"
fi
info "Syncing submodules..."
git -C "$REPO_DIR" submodule update --init --recursive || warn "Submodule sync failed (ignore if no submodules)"

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
info "Merging managed keys into $REPO_DIR/config.local.env..."
CONFIG_LOCAL="$REPO_DIR/config.local.env"
cp -f "$CONFIG_LOCAL" "$CONFIG_LOCAL.bak.agent-fleet" 2>/dev/null || true
BASE_URL="$BASE_URL" \
AUTH_TOKEN="$AUTH_TOKEN" \
MODEL="$MODEL" \
TRACE_TO_OPIK="${TRACE_TO_OPIK:-}" \
OPIK_URL="${OPIK_URL:-}" \
OPIK_API_KEY="${OPIK_API_KEY:-}" \
OPIK_WORKSPACE="${OPIK_WORKSPACE:-}" \
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-}" \
CONFIG_LOCAL="$CONFIG_LOCAL" \
  python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["CONFIG_LOCAL"])
base = os.environ["BASE_URL"].rstrip("/")
token = os.environ["AUTH_TOKEN"]
model = os.environ["MODEL"]

# Managed keys: BASE_URL stored without /v1 (repo convention; runners append it).
managed = {
    "BASE_URL": base,
    "API_KEY": token,
    "MODEL": model,
}
trace_to_opik = os.environ.get("TRACE_TO_OPIK", "").strip()
if trace_to_opik:
    managed["TRACE_TO_OPIK"] = trace_to_opik
opik_url = os.environ.get("OPIK_URL", "").strip()
if opik_url:
    managed["OPIK_URL"] = opik_url
    managed["OPIK_API_KEY"] = os.environ.get("OPIK_API_KEY", "")
    managed["OPIK_WORKSPACE"] = os.environ.get("OPIK_WORKSPACE") or "default"
    managed["OPIK_PROJECT_NAME"] = os.environ.get("OPIK_PROJECT_NAME", "")

# Read existing lines, keep non-managed keys as-is.
existing = {}
order = []
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            order.append(("comment", line.rstrip("\n")))
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            existing[k] = v
            order.append(("kv", k))
        else:
            order.append(("raw", line.rstrip("\n")))

existing.update(managed)

# Emit: existing structure (comments/order preserved) + any new managed keys.
# Values are plain (unquoted) to match config.env convention; the repo's
# env-file readers (load_env_file) use partition("=") + strip() without
# shell unquoting, so quoted values would be read back with the quotes
# still embedded.
def emit(k):
    return f"{k}={existing[k]}"

seen = set()
out = []
for kind, val in order:
    if kind == "kv":
        if val in existing:
            out.append(emit(val))
            seen.add(val)
    else:
        out.append(val)
for k in managed:
    if k not in seen:
        out.append(emit(k))

path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
ok "config.local.env merged; backup at ${CONFIG_LOCAL}.bak.agent-fleet"

# ---- 11. Docker permission check ----
info "Checking Docker permission..."
if docker ps >/dev/null 2>&1; then
  ok "Docker permission OK"
else
  warn "Current user has no Docker permission, but benchmark REQUIRES Docker!"
  warn "Fix: sudo usermod -aG docker \$USER  then reopen terminal/tmux"
fi

echo
ok "========================================"
ok " Environment setup complete!"
ok "========================================"
echo
info "Idempotent: safe to re-run if something failed."
info "In a new terminal, run 'source ~/.bashrc' first."
