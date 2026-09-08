#!/usr/bin/env bash
set -euo pipefail

# Dependency cache preparation, delivery, and worker readiness.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

harbor_wait_for_workers_ready() {
  while true; do
    if [[ -f "$WORKERS_READY_FILE" ]]; then
      harbor_apply_effective_wheel_source
      return 0
    fi
    [[ -f "$WORKERS_FAILED_FILE" ]] && return 1
    sleep 1
  done
}

harbor_ensure_local_wheels_server() {
  mkdir -p "$RUNTIME_DIR"
  local pid_file="${RUNTIME_DIR}/local-wheel-http.pid"
  local log_file="${RUNTIME_DIR}/local-wheel-http.log"
  local port pid attempt last_port

  [[ -d "$LOCAL_WHEEL_DIR" ]] || return 0

  last_port=$((LOCAL_WHEEL_PORT + LOCAL_WHEEL_PORT_ATTEMPTS - 1))
  for port in $(seq "$LOCAL_WHEEL_PORT" "$last_port"); do
    export HARBOR_LOCAL_WHEEL_SERVER_URL="http://${LOCAL_WHEEL_HOST_IP}:${port}"
    export HARBOR_LOCAL_CLAUDE_TGZ_URL="${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
    local agent_tgz
    agent_tgz="$(harbor_agent_tgz_basename)"

    # Treat wheel servers without the selected agent tgz as incomplete.
    local urls=("${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/manifest.txt" "${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${agent_tgz}")
    if harbor_agent_is_opencode; then
      urls+=("${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}")
    elif harbor_agent_is_pi; then
      urls+=("${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${PI_NODE_RUNTIME_BASENAME}")
    else
      urls+=("${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/npm-cache-ready")
    fi

    # Avoid probing a broad range of ports. Start on the preferred port first;
    # only if binding fails do a narrow readiness check to see whether another
    # monitor already owns a compatible wheel server on that exact port.
    nohup python3 -m http.server "$port" --directory "$LOCAL_WHEEL_DIR" \
      >"$log_file.${port}" 2>&1 &
    pid="$!"
    sleep 1

    local ready=1
    local url
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      ready=1
      for url in "${urls[@]}"; do
        if ! harbor_url_is_reachable "$url"; then
          ready=0
          break
        fi
      done
      if [[ "$ready" == "1" ]]; then
        echo "$port" > "${RUNTIME_DIR}/local-wheel-http.port"
        return 0
      fi
      continue
    fi

    for url in "${urls[@]}"; do
      if ! harbor_url_is_reachable "$url"; then
        ready=0
        break
      fi
    done
    if [[ "$ready" == "1" ]]; then
      echo "$pid" > "$pid_file"
      echo "$port" > "${RUNTIME_DIR}/local-wheel-http.port"
      return 0
    fi
    kill "$pid" >/dev/null 2>&1 || true
  done

  echo "failed to start a matching local wheel HTTP server in ${LOCAL_WHEEL_PORT_ATTEMPTS} attempts" >&2
  return 1
}

harbor_url_is_reachable() {
  local url="$1"
  python3 "$SCRIPT_DIR/env.py" url-reachable "$url"
}

harbor_manifest_url_ready() {
  local url="$1"
  python3 "$SCRIPT_DIR/env.py" manifest-url-ready "$url"
}

harbor_gzip_file_ready() {
  local path="$1"
  [[ -f "$path" ]] && gzip -t "$path" >/dev/null 2>&1
}

harbor_tar_file_ready() {
  local path="$1"
  python3 "$SCRIPT_DIR/env.py" tar-file-ready "$path" >/dev/null 2>&1
}

harbor_npm_tarball_version_ready() {
  local path="$1" expected_version="$2"
  python3 "$SCRIPT_DIR/env.py" npm-tarball-version-ready \
    "$path" "$expected_version" >/dev/null 2>&1
}

harbor_verifier_bundle_archive_ready() {
  local path="$1"
  [[ -f "$path" && -f "$VERIFIER_RUNTIME_BUNDLE_PREPARER" ]] \
    && python3 "$VERIFIER_RUNTIME_BUNDLE_PREPARER" \
      check --archive "$path" >/dev/null 2>&1
}

harbor_python_runtime_archive_ready() {
  local path="$1"
  [[ -f "$path" ]] \
    && python3 "$SCRIPT_DIR/python_runtime.py" \
      --check "$path" >/dev/null 2>&1
}

verifier_runtime_bundle_ready() {
  verifier_runtime_bundle_required \
    && harbor_verifier_bundle_archive_ready "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"
}

harbor_local_cache_ready() {
  [[ -f "$LOCAL_WHEEL_DIR/manifest.txt" ]] \
    && grep -qx 'cache_schema=3' "$LOCAL_WHEEL_DIR/manifest.txt" \
    && [[ "$(find "$LOCAL_WHEEL_DIR" -maxdepth 1 -name 'opik-*.whl' -type f | wc -l | tr -d ' ')" == "1" ]] \
    && [[ -f "$LOCAL_WHEEL_DIR/get-pip.py" ]] \
    && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/node-runtime.tar.xz" \
    && harbor_python_runtime_archive_ready "$LOCAL_WHEEL_DIR/python3.12-runtime.tar.gz" \
    && {
      if harbor_agent_is_opencode; then
        harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/${OPENCODE_TGZ_BASENAME}" \
          && harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/${OPENCODE_LINUX_X64_TGZ_BASENAME}"
      elif harbor_agent_is_pi; then
        harbor_gzip_file_ready "$LOCAL_WHEEL_DIR/${PI_TGZ_BASENAME}" \
          && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/${PI_NODE_RUNTIME_BASENAME}" \
          && harbor_tar_file_ready "$LOCAL_WHEEL_DIR/${PI_RUNTIME_BASENAME}" \
          && grep -qx "pi_runtime_version=${PI_VERSION}" "$LOCAL_WHEEL_DIR/manifest.txt"
      else
        harbor_npm_tarball_version_ready \
          "$LOCAL_WHEEL_DIR/${CLAUDE_CODE_TGZ_BASENAME}" \
          "$CLAUDE_CODE_VERSION" \
          && [[ -d "$LOCAL_WHEEL_DIR/npm-cache/_cacache" ]] \
          && grep -qx "$CLAUDE_CODE_VERSION" "$LOCAL_WHEEL_DIR/npm-cache-ready" \
          && grep -qx "claude_npm_cache_version=${CLAUDE_CODE_VERSION}" "$LOCAL_WHEEL_DIR/manifest.txt"
      fi
    }
}

harbor_pick_remote_wheel_url() {
  # Pi unpacks its pinned runtime from the read-only dependency mount. A
  # remote URL cannot materialize that mount, so Pi always prepares locally.
  if harbor_agent_is_pi; then
    return 1
  fi
  # Claude's npm package has platform-specific optional dependencies. The
  # remote cache path exposes only the top-level tarball to Docker trials, not
  # the accompanying npm cache, so it can silently fall back to an online npm
  # install in every task container. Prepare a complete local cache instead so
  # the tarball and npm cache are mounted together.
  if harbor_agent_is_claude_code \
    && [[ "$HARBOR_ENVIRONMENT_TYPE" == "docker" ]]; then
    return 1
  fi
  local candidates=()
  local candidate
  if [[ -n "${HARBOR_REMOTE_WHEEL_SERVER_URLS:-}" ]]; then
    IFS=',' read -r -a candidates <<< "$HARBOR_REMOTE_WHEEL_SERVER_URLS"
  elif [[ -n "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
    candidates=("$HARBOR_LOCAL_WHEEL_SERVER_URL")
  fi

  for candidate in "${candidates[@]}"; do
    candidate="${candidate%% }"
    candidate="${candidate## }"
    [[ -n "${candidate:-}" ]] || continue
    local agent_tgz
    agent_tgz="$(harbor_agent_tgz_basename)"
    local urls=("${candidate%/}/${agent_tgz}")
    if harbor_agent_is_opencode; then
      urls+=("${candidate%/}/${OPENCODE_LINUX_X64_TGZ_BASENAME}")
    else
      urls+=("${candidate%/}/npm-cache-ready")
    fi
    local ready=1
    local url
    local manifest_requirement=""
    if harbor_agent_is_pi; then
      manifest_requirement="pi_runtime_version=${PI_VERSION}"
    fi
    if ! harbor_manifest_url_ready "${candidate%/}/manifest.txt" "$manifest_requirement"; then
      ready=0
    fi
    for url in "${urls[@]}"; do
      if ! harbor_url_is_reachable "$url"; then
        ready=0
        break
      fi
    done
    if [[ "$ready" == "1" ]]; then
      printf '%s\n' "${candidate%/}"
      return 0
    fi
  done
  return 1
}

harbor_write_effective_wheel_source() {
  local wheel_url="$1"
  printf '%s\n' "$wheel_url" > "$EFFECTIVE_WHEEL_URL_FILE"
  printf '%s\n' "${wheel_url%/}/${CLAUDE_CODE_TGZ_BASENAME}" > "$EFFECTIVE_CLAUDE_TGZ_URL_FILE"
  export HARBOR_LOCAL_WHEEL_SERVER_URL="$wheel_url"
  export HARBOR_LOCAL_CLAUDE_TGZ_URL="${wheel_url%/}/${CLAUDE_CODE_TGZ_BASENAME}"
}

harbor_apply_effective_wheel_source() {
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" \
    || "$HARBOR_ENVIRONMENT_TYPE" == "qz" \
    || "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    # Managed Sandboxes do not provide Notebook-host bind mounts. Avoid making
    # their setup depend on inbound reachability to an ephemeral runner HTTP
    # server; use the configured Sandbox-side registry/dist source instead.
    unset HARBOR_LOCAL_WHEEL_SERVER_URL HARBOR_LOCAL_CLAUDE_TGZ_URL
    return 0
  fi
  if [[ -f "$EFFECTIVE_WHEEL_URL_FILE" ]]; then
    export HARBOR_LOCAL_WHEEL_SERVER_URL="$(cat "$EFFECTIVE_WHEEL_URL_FILE")"
  fi
  if [[ -f "$EFFECTIVE_CLAUDE_TGZ_URL_FILE" ]]; then
    export HARBOR_LOCAL_CLAUDE_TGZ_URL="$(cat "$EFFECTIVE_CLAUDE_TGZ_URL_FILE")"
  elif [[ -n "${HARBOR_LOCAL_WHEEL_SERVER_URL:-}" ]]; then
    export HARBOR_LOCAL_CLAUDE_TGZ_URL="${HARBOR_LOCAL_WHEEL_SERVER_URL%/}/${CLAUDE_CODE_TGZ_BASENAME}"
  fi
}

harbor_prewarm_s3_upload_sources() {
  local -a sources=("$@")
  echo "prewarming immutable OpenSandbox S3 objects..."
  if python3 "$SCRIPT_DIR/opensandbox_s3_upload.py" preflight \
    && python3 "$SCRIPT_DIR/opensandbox_s3_upload.py" prewarm "${sources[@]}"; then
    return 0
  fi
  if [[ "$YICLOUD_SANDBOX_UPLOAD_BACKEND" == "auto" ]]; then
    echo '[WARN] OpenSandbox S3 attempt failed backend=auto phase=prewarm; runtime will retry S3 and warn before any HTTP fallback' >&2
    return 0
  fi
  return 1
}

harbor_prewarm_s3_upload_cache() {
  if [[ "$HARBOR_ENVIRONMENT_TYPE" != "opensandbox" \
    || ( "$YICLOUD_SANDBOX_UPLOAD_BACKEND" != "s3" \
      && "$YICLOUD_SANDBOX_UPLOAD_BACKEND" != "auto" ) ]]; then
    return 0
  fi
  if [[ ! -d "$LOCAL_WHEEL_DIR" ]]; then
    echo "S3 upload requires a prepared local dependency cache: $LOCAL_WHEEL_DIR" >&2
    return 1
  fi

  local -a sources=("$LOCAL_WHEEL_DIR")
  case "$AGENT" in
    claude-code)
      [[ -f "$HARBOR_CC_CLAUDE_TGZ_SOURCE" ]] \
        && sources+=("$HARBOR_CC_CLAUDE_TGZ_SOURCE")
      [[ -f "$HARBOR_CC_HOOK_SOURCE" ]] && sources+=("$HARBOR_CC_HOOK_SOURCE")
      [[ -f "$HARBOR_CC_WEB_MCP_SOURCE" ]] && sources+=("$HARBOR_CC_WEB_MCP_SOURCE")
      ;;
    opencode)
      [[ -f "$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE" ]] \
        && sources+=("$TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE")
      [[ -f "$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE" ]] \
        && sources+=("$TRACE_PLUGIN_OPENCODE_HOOK_SOURCE")
      ;;
  esac
  if verifier_runtime_bundle_required && verifier_runtime_bundle_ready; then
    sources+=("$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE")
  fi
  harbor_prewarm_s3_upload_sources "${sources[@]}"
}

harbor_build_verifier_runtime_bundle() {
  verifier_runtime_bundle_required || return 0
  validate_verifier_runtime_bundle_transport || return 1
  verifier_runtime_bundle_ready && return 0

  echo "preparing verifier runtime bundle: $VERIFIER_RUNTIME_BUNDLE_ID"
  if [[ ! -f "$VERIFIER_RUNTIME_BUNDLE_PREPARER" ]] \
    || [[ ! -x "$HARBOR_OPIK_PYTHON" ]] \
    || ! PYTHON_BIN="$HARBOR_OPIK_PYTHON" \
      "$HARBOR_OPIK_PYTHON" "$VERIFIER_RUNTIME_BUNDLE_PREPARER" build \
    --cache-dir "$HARBOR_CC_PY_WHEEL_DIR_SOURCE" \
    --output "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"; then
    echo "failed to prepare verifier runtime bundle: $VERIFIER_RUNTIME_BUNDLE_ID" >&2
    return 1
  fi
  if ! verifier_runtime_bundle_ready; then
    echo "prepared verifier runtime bundle is invalid: $VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE" >&2
    return 1
  fi
}

harbor_prepare_verifier_runtime_bundle() {
  harbor_build_verifier_runtime_bundle || return 1
  verifier_runtime_bundle_required || return 0
  harbor_prewarm_s3_upload_sources "$VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE"
}

harbor_prepare_or_select_wheels() {
  validate_verifier_runtime_bundle_transport || return 1
  mkdir -p "$RUNTIME_DIR"
  local status_file="${RUNTIME_DIR}/local-deps-prepare.status"
  rm -f "$WORKERS_READY_FILE" "$WORKERS_FAILED_FILE" "$EFFECTIVE_WHEEL_URL_FILE" "$EFFECTIVE_CLAUDE_TGZ_URL_FILE" "$HARBOR_RUNNER_PREPARE_STATUS_FILE"
  : > "$LOCAL_DEPS_LOG_FILE"
  echo "checking" > "$status_file"

  if harbor_local_cache_ready; then
    echo "using local wheel cache"
    harbor_build_verifier_runtime_bundle || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    harbor_ensure_local_wheels_server
    harbor_write_effective_wheel_source "$HARBOR_LOCAL_WHEEL_SERVER_URL"
    harbor_prewarm_s3_upload_cache || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    echo "done" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  local remote_url
  remote_url="$(harbor_pick_remote_wheel_url || true)"
  if [[ -n "${remote_url:-}" ]]; then
    if [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" \
      && ( "$YICLOUD_SANDBOX_UPLOAD_BACKEND" == "s3" \
        || "$YICLOUD_SANDBOX_UPLOAD_BACKEND" == "auto" ) ]]; then
      echo "S3 upload cannot use a remote-only dependency cache" >&2
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    fi
    echo "using remote wheel cache: $remote_url"
    harbor_write_effective_wheel_source "$remote_url"
    echo "remote" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  echo "preparing" > "$status_file"
  echo "local cache missing; downloading dependency cache..."
  local prepare_opencode_cache=0 prepare_pi_cache=0
  if harbor_agent_is_opencode; then
    prepare_opencode_cache=1
  elif harbor_agent_is_pi; then
    prepare_pi_cache=1
  fi
  if (cd "$SCRIPT_DIR" && WHEEL_DIR="$LOCAL_WHEEL_DIR" CACHE_SCHEMA=3 CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" CLAUDE_CODE_TGZ_BASENAME="$CLAUDE_CODE_TGZ_BASENAME" PREPARE_OPENCODE_CACHE="$prepare_opencode_cache" OPENCODE_VERSION="$OPENCODE_VERSION" OPENCODE_TGZ_BASENAME="$OPENCODE_TGZ_BASENAME" OPENCODE_LINUX_X64_TGZ_BASENAME="$OPENCODE_LINUX_X64_TGZ_BASENAME" PREPARE_PI_CACHE="$prepare_pi_cache" PI_VERSION="$PI_VERSION" PI_TGZ_BASENAME="$PI_TGZ_BASENAME" PI_NODE_RUNTIME_BASENAME="$PI_NODE_RUNTIME_BASENAME" PI_RUNTIME_BASENAME="$PI_RUNTIME_BASENAME" ./prepare_local_deps.sh 2>&1 | tee -a "$LOCAL_DEPS_LOG_FILE"); then
    harbor_build_verifier_runtime_bundle || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    harbor_ensure_local_wheels_server
    harbor_write_effective_wheel_source "$HARBOR_LOCAL_WHEEL_SERVER_URL"
    harbor_prewarm_s3_upload_cache || {
      echo "failed" > "$status_file"
      touch "$WORKERS_FAILED_FILE"
      return 1
    }
    echo "done" > "$status_file"
    harbor_mark_workers_ready
    return $?
  fi

  echo "failed" > "$status_file"
  touch "$WORKERS_FAILED_FILE"
  return 1
}

harbor_prepare_agent_runtime() {
  if harbor_agent_is_oracle \
    || [[ "$ROLLOUT" == "1" && "$RL_AGENT" == "oracle" ]]; then
    mkdir -p "$RUNTIME_DIR"
    rm -f "$WORKERS_FAILED_FILE" "$HARBOR_RUNNER_PREPARE_STATUS_FILE"
    if harbor_prepare_verifier_runtime_bundle && harbor_validate_runner_cli; then
      touch "$WORKERS_READY_FILE"
      return 0
    fi
    touch "$WORKERS_FAILED_FILE"
    return 1
  fi

  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    mkdir -p "$RUNTIME_DIR"
    rm -f "$WORKERS_FAILED_FILE" "$HARBOR_RUNNER_PREPARE_STATUS_FILE"
    if harbor_validate_runner_cli; then
      touch "$WORKERS_READY_FILE"
      return 0
    fi
    touch "$WORKERS_FAILED_FILE"
    return 1
  fi

  if harbor_agent_is_opencode; then
    if ! harbor_prepare_or_select_wheels; then
      echo "failed to prepare local dependency cache" >&2
      touch "$WORKERS_FAILED_FILE"
      return 1
    fi
    return 0
  fi

  if ! harbor_prepare_or_select_wheels; then
    echo "failed to prepare local dependency cache" >&2
    touch "$WORKERS_FAILED_FILE"
    return 1
  fi
}

harbor_validate_runner_cli() {
  python3 "$SCRIPT_DIR/harbor_prepare_runner_cli.py"
}

harbor_runner_cli_ready() {
  if [[ "$HARBOR_RUNNER_PREPARE" != "1" ]]; then
    [[ -x "$HARBOR_OPIK_BIN" && -x "$HARBOR_CLI_BIN" ]]
    return
  fi
  [[ -x "$HARBOR_OPIK_BIN" ]] \
    && [[ -x "$HARBOR_CLI_BIN" ]] \
    && [[ -x "$HARBOR_OPIK_PYTHON" ]] \
    && [[ "$(cat "$HARBOR_RUNNER_PREPARE_STATUS_FILE" 2>/dev/null || true)" == "done" ]]
}

harbor_mark_workers_ready() {
  if harbor_validate_runner_cli; then
    touch "$WORKERS_READY_FILE"
    return 0
  fi
  return 1
}
