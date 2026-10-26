#!/usr/bin/env bash
set -euo pipefail

model_fusion_proxy_is_injectable() {
  [[ "${1:-}" == "harbor" && "${2:-}" == "run" ]] || return 1
  local arg
  for arg in "$@"; do
    [[ "$arg" != "--help" && "$arg" != "-h" ]] || return 1
  done
}

model_fusion_proxy_append_gateway_no_proxy() {
  local hostname_helper="$1"
  local base_url="$2"
  local gateway_host found_upper=0 found_lower=0 value i
  gateway_host="$(python3 "$hostname_helper" url-hostname "$base_url")"
  [[ -n "$gateway_host" ]] || return 0

  for ((i = 0; i + 1 < ${#args[@]}; i++)); do
    [[ "${args[$i]}" == "--ae" ]] || continue
    case "${args[$((i + 1))]}" in
      NO_PROXY=*)
        found_upper=1
        value="${args[$((i + 1))]#NO_PROXY=}"
        [[ ",$value," == *",$gateway_host,"* ]] \
          || args[$((i + 1))]="NO_PROXY=${value:+$value,}$gateway_host"
        ;;
      no_proxy=*)
        found_lower=1
        value="${args[$((i + 1))]#no_proxy=}"
        [[ ",$value," == *",$gateway_host,"* ]] \
          || args[$((i + 1))]="no_proxy=${value:+$value,}$gateway_host"
        ;;
    esac
  done
  [[ "$found_upper" == "1" ]] || args+=(--ae "NO_PROXY=$gateway_host")
  [[ "$found_lower" == "1" ]] || args+=(--ae "no_proxy=$gateway_host")
}

model_fusion_proxy_merge_readonly_mounts() {
  local worker_utils="$1"
  shift
  local mount_value_index=-1 mounts_json="[]" i
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--mounts-json" ]]; then
      if ((i + 1 >= ${#args[@]})); then
        echo "[ERROR] --mounts-json is missing its value" >&2
        return 2
      fi
      mount_value_index=$((i + 1))
      break
    fi
  done
  if ((mount_value_index >= 0)); then
    mounts_json="${args[$mount_value_index]}"
  fi
  mounts_json="$(
    python3 "$worker_utils" append-readonly-mounts "$mounts_json" "$@"
  )"
  if ((mount_value_index >= 0)); then
    args[$mount_value_index]="$mounts_json"
  else
    args+=(--mounts-json "$mounts_json")
  fi
}

model_fusion_proxy_render_or_exec() {
  local label="$1"
  local real_opik_bin="$2"
  local worker_utils="$3"
  if [[ "${MODEL_FUSION_PROXY_RENDER_ONLY:-0}" == "1" ]]; then
    local rendered_command
    rendered_command="$(
      python3 "$worker_utils" render-command "$real_opik_bin" "${args[@]}"
    )"
    printf '[%s] proxy command: %s\n' "$label" "$rendered_command"
    return 0
  fi
  exec "$real_opik_bin" "${args[@]}"
}
