#!/usr/bin/env bash
set -euo pipefail

# Shared repository configuration loader. This is a sourced library, not an
# entry point. It resolves source precedence only; compatibility fallbacks are
# opt-in so tool-specific aliases remain scoped to the tool that owns them.

agent_fleet_load_config() {
  local repo_root="$1"
  local entry file name
  local -a caller_env=()

  if [[ "${AGENT_FLEET_CONFIG_LOADED_ROOT:-}" == "$repo_root" ]]; then
    return 0
  fi

  # Save the runtime/exported environment before loading saved configuration.
  # compgen is a Bash builtin and works on the older Bash shipped by macOS;
  # avoid depending on the GNU-specific `env -0` option.
  while IFS= read -r name; do
    caller_env+=("$name=${!name-}")
  done < <(compgen -e)

  # Base template first, private saved configuration second.
  for file in "$repo_root/config.env" "$repo_root/config.local.env"; do
    if [[ -f "$file" ]]; then
      set -a
      # shellcheck source=/dev/null
      . "$file"
      set +a
    fi
  done

  # Runtime/exported values are the highest-priority layer.
  for entry in "${caller_env[@]}"; do
    name="${entry%%=*}"
    [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$entry"
  done

  AGENT_FLEET_CONFIG_LOADED_ROOT="$repo_root"
  export AGENT_FLEET_CONFIG_LOADED_ROOT

  agent_fleet_warn_retired_opik_vars
}

# OPIK_URL is the single switch for Opik tracing: an endpoint means traces are
# uploaded, an empty value means they are not. TRACE_TO_OPIK, OPIK_PLUGIN, and
# OPIK_MODE used to gate the same thing and were always derived from the URL in
# practice. A stale value in config.local.env or a CI job would now be ignored
# silently, so say so once instead. Tools whose stdout is parsed (the Harbor
# controller emits JSON) set AGENT_FLEET_CONFIG_QUIET=1.
#
# The marker is exported so a launcher and the runner it execs warn once
# between them rather than once each.
agent_fleet_warn_retired_opik_vars() {
  local name warned=0
  if [[ "${AGENT_FLEET_CONFIG_QUIET:-0}" == "1" ]] ||
     [[ -n "${AGENT_FLEET_OPIK_DEPRECATION_WARNED:-}" ]]; then
    return 0
  fi

  for name in TRACE_TO_OPIK OPIK_PLUGIN OPIK_MODE; do
    if [[ -n "${!name:-}" ]]; then
      echo "[WARN] $name is no longer used; Opik tracing follows OPIK_URL" >&2
      warned=1
    fi
  done

  if (( warned )); then
    echo "[WARN] set OPIK_URL to upload traces, or leave it empty to disable" >&2
    AGENT_FLEET_OPIK_DEPRECATION_WARNED=1
    export AGENT_FLEET_OPIK_DEPRECATION_WARNED
  fi
}

# Tracing is on when an Opik endpoint is configured. Sourced by every entry
# point so the shell and Python paths agree.
agent_fleet_opik_enabled() {
  [[ -n "${OPIK_URL:-}" ]]
}

# AUTH_TOKEN is a documented fleet-runner credential alias, not a general
# replacement for API_KEY. Apply it only after canonical configuration has
# loaded so a saved or runtime API_KEY keeps precedence.
agent_fleet_apply_auth_token_fallback() {
  if [[ -z "${API_KEY:-}" && -n "${AUTH_TOKEN:-}" ]]; then
    API_KEY="$AUTH_TOKEN"
    export API_KEY
  fi
}
