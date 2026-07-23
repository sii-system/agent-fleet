#!/usr/bin/env bash
# Set up the pinned Harbor control-runner environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HARBOR_RUNNER_UV_VERSION="${HARBOR_RUNNER_UV_VERSION:-0.11.28}"
HARBOR_RUNNER_PYTHON="${HARBOR_RUNNER_PYTHON:-3.12.13}"
HARBOR_RUNNER_IMAGE_DIR="${HARBOR_RUNNER_IMAGE_DIR:-/opt/harbor-runner}"
HARBOR_RUNNER_HOST_DIR="${HARBOR_RUNNER_HOST_DIR:-$HOME/.local/share/agent-fleet/harbor-runner}"
HARBOR_RUNNER_REQUIREMENTS="${HARBOR_RUNNER_REQUIREMENTS:-$SCRIPT_DIR/runner-requirements.txt}"
HARBOR_RUNNER_VALIDATOR="${HARBOR_RUNNER_VALIDATOR:-$SCRIPT_DIR/harbor_prepare_runner_cli.py}"
HARBOR_RUNNER_UV_INSTALL_DIR="${HARBOR_RUNNER_UV_INSTALL_DIR:-$HOME/.local/share/agent-fleet/uv/$HARBOR_RUNNER_UV_VERSION}"
HARBOR_RUNNER_UV_BIN="${HARBOR_RUNNER_UV_BIN:-}"
HARBOR_RUNNER_UV_INSTALLER_URL="${HARBOR_RUNNER_UV_INSTALLER_URL:-https://releases.astral.sh/github/uv/releases/download/$HARBOR_RUNNER_UV_VERSION/uv-installer.sh}"
HARBOR_RUNNER_PYTHON_MIRROR="${HARBOR_RUNNER_PYTHON_MIRROR:-https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download}"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[ OK ] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*" >&2; }

for required_file in "$HARBOR_RUNNER_REQUIREMENTS" "$HARBOR_RUNNER_VALIDATOR"; do
  if [[ ! -f "$required_file" ]]; then
    fail "Harbor runner setup input not found: $required_file"
    exit 1
  fi
done
if ! command -v python3 >/dev/null 2>&1; then
  fail "Harbor runner setup requires command: python3"
  exit 1
fi

validate_runner() {
  local runner_dir="$1"
  HARBOR_OPIK_BIN="$runner_dir/bin/opik" \
  HARBOR_CLI_BIN="$runner_dir/bin/harbor" \
  HARBOR_OPIK_PYTHON="$runner_dir/bin/python" \
  HARBOR_RUNNER_PYTHON_VERSION="$HARBOR_RUNNER_PYTHON" \
  HARBOR_RUNNER_REQUIREMENTS="$HARBOR_RUNNER_REQUIREMENTS" \
    python3 "$HARBOR_RUNNER_VALIDATOR" --validate
}

if [[ -d "$HARBOR_RUNNER_IMAGE_DIR" ]]; then
  info "Validating image-provided Harbor runner: $HARBOR_RUNNER_IMAGE_DIR"
  if ! validate_runner "$HARBOR_RUNNER_IMAGE_DIR"; then
    fail "Image-provided Harbor runner does not match $HARBOR_RUNNER_REQUIREMENTS"
    exit 1
  fi
  ok "Image-provided Harbor runner is ready"
  exit 0
fi

if validate_runner "$HARBOR_RUNNER_HOST_DIR" >/dev/null 2>&1; then
  ok "Pinned host Harbor runner is already ready: $HARBOR_RUNNER_HOST_DIR"
  exit 0
fi

if [[ -z "$HARBOR_RUNNER_UV_BIN" ]]; then
  HARBOR_RUNNER_UV_BIN="$(command -v uv 2>/dev/null || true)"
fi
if [[ -z "$HARBOR_RUNNER_UV_BIN" ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    fail "Harbor runner setup requires curl to install uv"
    exit 1
  fi
  info "Installing uv $HARBOR_RUNNER_UV_VERSION for Harbor runner setup"
  curl --proto '=https' --tlsv1.2 -LsSf "$HARBOR_RUNNER_UV_INSTALLER_URL" \
    | env UV_UNMANAGED_INSTALL="$HARBOR_RUNNER_UV_INSTALL_DIR" sh
  HARBOR_RUNNER_UV_BIN="$HARBOR_RUNNER_UV_INSTALL_DIR/uv"
fi
if [[ ! -x "$HARBOR_RUNNER_UV_BIN" ]]; then
  fail "uv is not executable: $HARBOR_RUNNER_UV_BIN"
  exit 1
fi

info "Creating pinned host Harbor runner: $HARBOR_RUNNER_HOST_DIR"
if ! UV_PYTHON_INSTALL_MIRROR="$HARBOR_RUNNER_PYTHON_MIRROR" \
  "$HARBOR_RUNNER_UV_BIN" venv \
    --clear \
    --python "$HARBOR_RUNNER_PYTHON" \
    "$HARBOR_RUNNER_HOST_DIR"; then
  info "Python mirror failed; falling back to the upstream release"
  "$HARBOR_RUNNER_UV_BIN" venv \
    --clear \
    --python "$HARBOR_RUNNER_PYTHON" \
    "$HARBOR_RUNNER_HOST_DIR"
fi
UV_LINK_MODE=copy "$HARBOR_RUNNER_UV_BIN" pip install \
  --only-binary :all: \
  --python "$HARBOR_RUNNER_HOST_DIR/bin/python" \
  --requirement "$HARBOR_RUNNER_REQUIREMENTS"
"$HARBOR_RUNNER_UV_BIN" pip check \
  --python "$HARBOR_RUNNER_HOST_DIR/bin/python"

if ! validate_runner "$HARBOR_RUNNER_HOST_DIR"; then
  fail "Pinned host Harbor runner validation failed"
  exit 1
fi
ok "Pinned host Harbor runner is ready: $HARBOR_RUNNER_HOST_DIR"
