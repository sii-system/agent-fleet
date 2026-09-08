#!/usr/bin/env bash
set -euo pipefail

# OpenCode runtime secrets and generated provider configuration.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

# ── opencode agent ────────────────────────────────────────────────────────────
OPENCODE_VERSION="${OPENCODE_VERSION:-latest}"
OPENCODE_TGZ_BASENAME="${OPENCODE_TGZ_BASENAME:-opencode-ai-${OPENCODE_VERSION}.tgz}"
OPENCODE_LINUX_X64_TGZ_BASENAME="${OPENCODE_LINUX_X64_TGZ_BASENAME:-opencode-linux-x64-${OPENCODE_VERSION}.tgz}"
OPENCODE_CONFIG_CONTENT="${OPENCODE_CONFIG_CONTENT:-}"
OPENCODE_RUNTIME_SECRETS_JSON="${OPENCODE_RUNTIME_SECRETS_JSON:-}"
if [[ -z "$OPENCODE_RUNTIME_SECRETS_JSON" ]]; then
  OPENCODE_RUNTIME_SECRETS_JSON="{}"
fi
OPENCODE_HAS_REQUEST_HEADERS="0"
if [[ "$AGENT" == "opencode" ]]; then
  OPENCODE_RUNTIME_SECRETS_JSON="$(
    OPENCODE_RUNTIME_SECRETS_JSON="$OPENCODE_RUNTIME_SECRETS_JSON" \
    HARBOR_ANTHROPIC_AUTH_TOKEN="$HARBOR_ANTHROPIC_AUTH_TOKEN" \
    HARBOR_LLM_KWARGS="$HARBOR_LLM_KWARGS" \
    HARBOR_MODEL="$HARBOR_MODEL" \
    OPENCODE_CONFIG_CONTENT="$OPENCODE_CONFIG_CONTENT" \
      python3 "$SCRIPT_DIR/env.py" opencode-runtime-secrets
  )"
  if [[ -n "$OPENCODE_CONFIG_CONTENT" ]]; then
    OPENCODE_CONFIG_CONTENT="$(
      OPENCODE_CONFIG_CONTENT="$OPENCODE_CONFIG_CONTENT" \
        python3 "$SCRIPT_DIR/env.py" opencode-sanitize-config
    )"
  fi
  OPENCODE_HAS_REQUEST_HEADERS="$(
    HARBOR_LLM_KWARGS="$HARBOR_LLM_KWARGS" \
      python3 "$SCRIPT_DIR/env.py" has-model-request-headers
  )"
fi
if [[ "$AGENT" == "opencode" \
  && ( ( -z "$OPENCODE_CONFIG_CONTENT" && "${HARBOR_MODEL%%/*}" == "custom" ) \
    || -n "$HARBOR_TEMPERATURE" \
    || -n "$HARBOR_TOP_P" \
    || -n "$HARBOR_MAX_TOKENS" \
    || "$OPENCODE_HAS_REQUEST_HEADERS" == "1" ) ]]; then
  # OpenCode's built-in minimax provider ignores our gateway BASE_URL and calls
  # api.minimax.io directly. Use an OpenAI-compatible custom provider by default.
  OPENCODE_CONFIG_CONTENT="$(
    HARBOR_ANTHROPIC_BASE_URL="$HARBOR_ANTHROPIC_BASE_URL" \
    HARBOR_LLM_KWARGS="$HARBOR_LLM_KWARGS" \
    HARBOR_MODEL="$HARBOR_MODEL" \
    HARBOR_TEMPERATURE="$HARBOR_TEMPERATURE" \
    HARBOR_TOP_P="$HARBOR_TOP_P" \
    HARBOR_MAX_TOKENS="$HARBOR_MAX_TOKENS" \
    OPENCODE_CONFIG_CONTENT="$OPENCODE_CONFIG_CONTENT" \
      python3 "$SCRIPT_DIR/env.py" opencode-config
  )"
fi
