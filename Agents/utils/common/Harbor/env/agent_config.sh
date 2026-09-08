#!/usr/bin/env bash
set -euo pipefail

# Agent settings, generated OpenCode configuration, predicates, and validation.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

# Harbor CLI defaults. Keep adapter-specific values here so harboropik.sh and
# the zellij worker scripts cannot drift into different model/network settings.
HARBOR_LIMIT="${HARBOR_LIMIT:-}"
HARBOR_RUNS="${HARBOR_RUNS:-$N_ATTEMPTS}"
HARBOR_AGENT_IMPORT_PATH="${HARBOR_AGENT_IMPORT_PATH:-}"
HARBOR_MODEL="${HARBOR_MODEL:-$_HARBOR_EFFECTIVE_MODEL}"
if [[ "$AGENT" == "pi" && -z "$HARBOR_AGENT_IMPORT_PATH" ]]; then
  HARBOR_AGENT_IMPORT_PATH="pi_harbor:AgentFleetPi"
fi
if [[ "$AGENT" == "pi" && -z "$PI_PROVIDER" && -n "$HARBOR_ANTHROPIC_BASE_URL" ]]; then
  # Keep the Pi provider name tied to the gateway host. env.py uses the same
  # derivation while rendering models.json, so the CLI model and config agree.
  PI_PROVIDER="$(python3 - "$HARBOR_ANTHROPIC_BASE_URL" <<'PY'
from urllib.parse import urlparse
import sys

print((urlparse(sys.argv[1]).hostname or "").lower())
PY
)"
fi
if [[ "$AGENT" == "opencode" && "$HARBOR_MODEL" != */* && -n "$OPENCODE_PROVIDER" ]]; then
  HARBOR_MODEL="${OPENCODE_PROVIDER}/${HARBOR_MODEL}"
fi
if [[ "$AGENT" == "pi" && "$HARBOR_MODEL" != */* && -n "$PI_PROVIDER" ]]; then
  HARBOR_MODEL="${PI_PROVIDER}/${HARBOR_MODEL}"
fi
INCLUDE_TASKS="${INCLUDE_TASKS:-${HARBOR_INCLUDE_TASKS:-}}"
HARBOR_DRY_RUN="${HARBOR_DRY_RUN:-0}"
MIN_TEST_INCLUDE_TASK="${MIN_TEST_INCLUDE_TASK:-fix-git}"
HARBOR_N_CONCURRENT="${HARBOR_N_CONCURRENT:-$TOTAL_WORKERS}"
HARBOR_MAX_RETRIES="${HARBOR_MAX_RETRIES:-$MAX_RETRIES}"
HARBOR_RETRY_INCLUDE_EXCEPTIONS="${HARBOR_RETRY_INCLUDE_EXCEPTIONS-}"
HARBOR_RETRY_EXCLUDE_EXCEPTIONS="${HARBOR_RETRY_EXCLUDE_EXCEPTIONS-RewardFileNotFoundError,RewardFileEmptyError,VerifierOutputParseError}"
HARBOR_AK_MAX_TURNS="${HARBOR_AK_MAX_TURNS:-}"
HARBOR_AK_COLLECT_ROLLOUT_DETAILS="${HARBOR_AK_COLLECT_ROLLOUT_DETAILS:-}"
HARBOR_AK_ENABLE_SUMMARIZE="${HARBOR_AK_ENABLE_SUMMARIZE:-}"
HARBOR_DISALLOWED_TOOLS="${HARBOR_DISALLOWED_TOOLS:-WebSearch WebFetch RemoteTrigger AskUserQuestion}"
HARBOR_APPEND_SYSTEM_PROMPT="${HARBOR_APPEND_SYSTEM_PROMPT:-Use English only for all reasoning, messages, filenames, and tool arguments. Use ASCII characters only unless reading existing non-ASCII file contents is strictly necessary.}"
HARBOR_API_BASE="${HARBOR_API_BASE:-${HARBOR_ANTHROPIC_BASE_URL%/}/v1/chat/completions}"
ROLLOUT="${ROLLOUT:-0}"
HARBOR_TEMPERATURE="${HARBOR_TEMPERATURE:-}"
HARBOR_TOP_P="${HARBOR_TOP_P:-}"
HARBOR_MAX_TOKENS="${HARBOR_MAX_TOKENS:-}"
if [[ "$ROLLOUT" == "1" ]]; then
  HARBOR_TEMPERATURE=""
  HARBOR_TOP_P=""
  HARBOR_MAX_TOKENS=""
fi
PI_MODELS_CONFIG="${PI_MODELS_CONFIG:-}"
PI_SETTINGS_CONFIG="${PI_SETTINGS_CONFIG:-}"
PI_SUPPORTS_REASONING_EFFORT="${PI_SUPPORTS_REASONING_EFFORT:-}"
PI_THINKING_LEVEL_MAP="${PI_THINKING_LEVEL_MAP:-}"
PI_EXTENSION_SOURCE="${PI_EXTENSION_SOURCE:-$AGENTS_DIR/Harbor-pi/extensions}"
PI_EXTENSION_DIR="${PI_EXTENSION_DIR:-/opt/tb-pi/extensions}"
if [[ "$AGENT" == "pi" ]]; then
  if [[ -z "$PI_MODELS_CONFIG" ]]; then
    PI_MODELS_CONFIG="$(
      PI_PROVIDER="$PI_PROVIDER" \
      HARBOR_MODEL="$HARBOR_MODEL" \
      HARBOR_ANTHROPIC_BASE_URL="$HARBOR_ANTHROPIC_BASE_URL" \
      HARBOR_MAX_TOKENS="$HARBOR_MAX_TOKENS" \
      PI_SUPPORTS_REASONING_EFFORT="$PI_SUPPORTS_REASONING_EFFORT" \
      PI_THINKING_LEVEL_MAP="$PI_THINKING_LEVEL_MAP" \
        python3 "$SCRIPT_DIR/env.py" pi-models-config
    )"
  fi
  if [[ -z "$PI_SETTINGS_CONFIG" ]]; then
    PI_SETTINGS_CONFIG="$(
      PI_PROVIDER="$PI_PROVIDER" \
      HARBOR_MODEL="$HARBOR_MODEL" \
      PI_THINKING_LEVEL="$PI_THINKING_LEVEL" \
        python3 "$SCRIPT_DIR/env.py" pi-settings-config
    )"
  fi
fi
if [[ -z "${HARBOR_LLM_KWARGS:-}" ]]; then
  HARBOR_LLM_KWARGS="$(
    HARBOR_ANTHROPIC_AUTH_TOKEN="$HARBOR_ANTHROPIC_AUTH_TOKEN" \
    HARBOR_TEMPERATURE="$HARBOR_TEMPERATURE" \
    HARBOR_TOP_P="$HARBOR_TOP_P" \
      python3 "$SCRIPT_DIR/env.py" llm-kwargs
  )"
fi
_HARBOR_OUTPUT_TOKEN_LIMIT="${HARBOR_MAX_TOKENS:-65536}"
HARBOR_MAX_NEW_TOKENS="${HARBOR_MAX_NEW_TOKENS:-$_HARBOR_OUTPUT_TOKEN_LIMIT}"
HARBOR_MODEL_INFO="${HARBOR_MODEL_INFO:-}"
if [[ -z "$HARBOR_MODEL_INFO" ]]; then
  HARBOR_MODEL_INFO="$(
    _HARBOR_OUTPUT_TOKEN_LIMIT="$_HARBOR_OUTPUT_TOKEN_LIMIT" \
      python3 "$SCRIPT_DIR/env.py" model-info
  )"
fi
HARBOR_ANTHROPIC_CUSTOM_HEADERS="${HARBOR_ANTHROPIC_CUSTOM_HEADERS:-${ANTHROPIC_CUSTOM_HEADERS:-}}"
HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS="${HARBOR_CLAUDE_CODE_MAX_OUTPUT_TOKENS:-$_HARBOR_OUTPUT_TOKEN_LIMIT}"
HARBOR_CLAUDE_CODE_DISABLE_AUTOUPDATER="${HARBOR_CLAUDE_CODE_DISABLE_AUTOUPDATER:-1}"

# Advanced Claude Code model routing defaults follow Harbor's effective task
# model. This keeps direct HARBOR_MODEL compatibility scoped to Harbor without
# promoting it into the repository-wide MODEL variable.
HARBOR_ANTHROPIC_MODEL="${HARBOR_ANTHROPIC_MODEL:-$HARBOR_MODEL}"
HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL="${HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL:-$HARBOR_MODEL}"
HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL="${HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL:-$HARBOR_MODEL}"
HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL="${HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$HARBOR_MODEL}"
HARBOR_CLAUDE_CODE_SUBAGENT_MODEL="${HARBOR_CLAUDE_CODE_SUBAGENT_MODEL:-$HARBOR_MODEL}"
HARBOR_CLAUDE_CODE_EFFORT_LEVEL="${HARBOR_CLAUDE_CODE_EFFORT_LEVEL:-max}"

HARBOR_TIMEOUT_MULTIPLIER="${HARBOR_TIMEOUT_MULTIPLIER:-3.0}"
# Overrides only the agent execution timeout. Leave empty to use HARBOR_TIMEOUT_MULTIPLIER.
HARBOR_AGENT_TIMEOUT_MULTIPLIER="${HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}"
HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER="${HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER:-20}"
# Set to 1 only when Harbor prebuilt task images fail to pull from registry mirrors;
# this bypasses prebuilt pulls and builds from each task's local Dockerfile instead.
HARBOR_FORCE_BUILD="${HARBOR_FORCE_BUILD:-0}"
HARBOR_DEBUG="${HARBOR_DEBUG:-0}"
# The realtime hook default follows the tracing switch: without an Opik
# server there is nothing for the hook to talk to. An explicit value wins.
if harbor_trace_to_opik_enabled; then
  HARBOR_CC_OPIK_ENABLE_HOOK="${HARBOR_CC_OPIK_ENABLE_HOOK:-1}"
else
  HARBOR_CC_OPIK_ENABLE_HOOK="${HARBOR_CC_OPIK_ENABLE_HOOK:-0}"
fi
TRACE_PLUGIN_SOURCE_DIR="${TRACE_PLUGIN_SOURCE_DIR:-$REPO_ROOT/third_party/agent-opik-plugin}"
TRACE_PLUGIN_CLAUDE_HOOK_SOURCE="${TRACE_PLUGIN_CLAUDE_HOOK_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/src/sii_opik_plugin/claude_code/claude_realtime_trace.py}"
TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE="${TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/harness/opencode/opik-trace.ts}"
TRACE_PLUGIN_OPENCODE_HOOK_SOURCE="${TRACE_PLUGIN_OPENCODE_HOOK_SOURCE:-$TRACE_PLUGIN_SOURCE_DIR/src/sii_opik_plugin/opencode/opencode_realtime_trace.py}"
HARBOR_CC_HOOK_SOURCE="${HARBOR_CC_HOOK_SOURCE:-$TRACE_PLUGIN_CLAUDE_HOOK_SOURCE}"
HARBOR_CC_HOOK_MOUNT_PATH="${HARBOR_CC_HOOK_MOUNT_PATH:-/opt/tb-opik/claude_realtime_trace.py}"
HARBOR_CC_CLAUDE_TGZ_SOURCE="${HARBOR_CC_CLAUDE_TGZ_SOURCE:-${LOCAL_WHEEL_DIR}/${CLAUDE_CODE_TGZ_BASENAME}}"
HARBOR_CC_CLAUDE_TGZ_MOUNT_PATH="${HARBOR_CC_CLAUDE_TGZ_MOUNT_PATH:-/opt/tb-opik/claude-code.tgz}"
HARBOR_CC_PY_WHEEL_DIR_SOURCE="${HARBOR_CC_PY_WHEEL_DIR_SOURCE:-$LOCAL_WHEEL_DIR}"
HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH="${HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH:-/opt/tb-opik/python-wheels}"
HARBOR_CC_NPM_CACHE_MOUNT_PATH="${HARBOR_CC_NPM_CACHE_MOUNT_PATH:-${HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH}/npm-cache}"
HARBOR_CC_WEB_MCP_SOURCE="${HARBOR_CC_WEB_MCP_SOURCE:-}"
HARBOR_CC_WEB_MCP_MOUNT_PATH="${HARBOR_CC_WEB_MCP_MOUNT_PATH:-/opt/agent-fleet/exa_web_mcp.py}"
HARBOR_VERIFIER_UV_HOME="${HARBOR_VERIFIER_UV_HOME:-}"
HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH="${HARBOR_VERIFIER_UV_BIN_DIR_MOUNT_PATH:-/opt/tb-uv-backup/bin}"
HARBOR_E2B_VERIFIER_UV_SOURCE="${HARBOR_E2B_VERIFIER_UV_SOURCE:-}"
HARBOR_CC_OPIK_DEBUG="${HARBOR_CC_OPIK_DEBUG:-$CC_OPIK_DEBUG}"
HARBOR_CC_OPIK_INSTALL_DEPS="${HARBOR_CC_OPIK_INSTALL_DEPS:-true}"
# Package-source canonical name. Resolve its backend-specific default after
# HARBOR_ENVIRONMENT_TYPE is known; explicit saved or runtime values remain intact.
NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-}"
GO111MODULE="${GO111MODULE:-on}"
GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
GOSUMDB="${GOSUMDB:-sum.golang.google.cn}"
RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup}"
RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://mirrors.tuna.tsinghua.edu.cn/rustup}"
CARGO_REGISTRY_REPLACE_WITH="${CARGO_REGISTRY_REPLACE_WITH:-mirror}"
CARGO_REGISTRY_URL="${CARGO_REGISTRY_URL:-sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple/}"
PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-}"

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

harbor_agent_is_opencode() {
  [[ "$AGENT" == "opencode" ]]
}

harbor_agent_is_claude_code() {
  [[ "$AGENT" == "claude-code" ]]
}

harbor_agent_is_pi() {
  [[ "$AGENT" == "pi" ]]
}

harbor_agent_tgz_basename() {
  if harbor_agent_is_opencode; then
    printf '%s\n' "$OPENCODE_TGZ_BASENAME"
  elif harbor_agent_is_pi; then
    printf '%s\n' "$PI_RUNTIME_BASENAME"
  else
    printf '%s\n' "$CLAUDE_CODE_TGZ_BASENAME"
  fi
}

harbor_validate_generation_controls() {
  if [[ -n "$HARBOR_MAX_TOKENS" && ! "$HARBOR_MAX_TOKENS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] HARBOR_MAX_TOKENS must be a positive integer." >&2
    return 1
  fi
  if [[ "$ROLLOUT" != "1" ]] \
    && { harbor_agent_is_claude_code || harbor_agent_is_pi; } \
    && [[ -n "$HARBOR_TEMPERATURE" || -n "$HARBOR_TOP_P" ]]; then
    local agent_display="Claude Code"
    if harbor_agent_is_pi; then
      agent_display="Pi"
    fi
    echo "[ERROR] $agent_display does not expose temperature or top_p controls." >&2
    echo "[ERROR] Use AGENT=opencode for these settings, or leave them unset." >&2
    return 1
  fi
}

harbor_agent_is_oracle() {
  [[ "$AGENT" == "oracle" ]]
}

harbor_validate_agent() {
  case "$AGENT" in
    claude-code|opencode|pi|oracle) ;;
    *)
      echo "[ERROR] AGENT must be claude-code, opencode, pi, or oracle, got: $AGENT" >&2
      exit 1
      ;;
  esac

  if harbor_agent_is_opencode; then
    if [[ -z "$OPENCODE_CONFIG_CONTENT" ]]; then
      echo "[WARN] AGENT=opencode but OPENCODE_CONFIG_CONTENT is empty;" >&2
      echo "[WARN] opencode will fall back to ANTHROPIC_* env if provided." >&2
    fi
  fi
  if harbor_agent_is_pi; then
    case "$PI_THINKING_LEVEL" in
      off|minimal|low|medium|high|xhigh|max) ;;
      *)
        echo "[ERROR] PI_THINKING_LEVEL must be off, minimal, low, medium, high, xhigh, or max." >&2
        exit 1
        ;;
    esac
  fi
}
