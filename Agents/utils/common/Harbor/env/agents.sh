#!/usr/bin/env bash
set -euo pipefail

# Agent predicates and configuration validation.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

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
