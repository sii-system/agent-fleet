#!/usr/bin/env bash
set -euo pipefail

RL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_SCRIPT_DIR="${HARBOR_SCRIPT_DIR:-$(cd "$RL_SCRIPT_DIR/../common/Harbor" && pwd)}"
. "$HARBOR_SCRIPT_DIR/env.sh"

ray_submission_id="${1:-${RL_ZELLIJ_SUBMISSION_ID:?ray submission id required}}"
dataset_name="${2:-${RL_DATASET_NAME:-seta}}"
job_queue_dir="${3:-${RL_ZELLIJ_JOB_QUEUE_DIR:-${RL_JOB_QUEUE_ROOT}/$(printf '%s' "$ray_submission_id" | tr -c 'A-Za-z0-9._-' '-')}}"

safe_name() {
  printf '%s' "$1" | tr '/[:space:]' '---' | tr -cd 'A-Za-z0-9._-'
}

ensure_zellij_web_sharing_config() {
  local config_file="${ZELLIJ_CONFIG_FILE:-$HOME/.config/zellij/config.kdl}"
  mkdir -p "$(dirname "$config_file")"
  if [[ -f "$config_file" ]] && grep -qE '^[[:space:]]*web_sharing[[:space:]]+' "$config_file"; then
    sed -i -E 's/^[[:space:]]*web_sharing[[:space:]]+".*"$/web_sharing "on"/' "$config_file"
  else
    printf '\nweb_sharing "on"\n' >> "$config_file"
  fi
}

submission_storage_id="${RL_ZELLIJ_SUBMISSION_STORAGE_ID:-$ray_submission_id}"
submission_slug="$(safe_name "$submission_storage_id")"
agent_slug="$(safe_name "${RL_AGENT:-claude-code}")"
session_name="${RL_ZELLIJ_SESSION_NAME:-}"
if [[ -z "$session_name" ]]; then
  # Standalone fallback matching rollout_remote_harbor.py. The normal listener
  # path passes RL_ZELLIJ_SESSION_NAME explicitly so one side owns the value.
  session_digest="$(
    printf '%s\0%s\0%s\0%s\0%s' \
      "${RL_AGENT:-claude-code}" "$dataset_name" "$RL_JOB_QUEUE_ROOT" \
      "$RL_JOB_RUNTIME_ROOT" "$ray_submission_id" \
      | sha256sum | cut -c1-32
  )"
  session_name="hr-${session_digest}"
fi
if [[ ! "$session_name" =~ ^hr-[0-9a-f]{32}$ ]]; then
  echo "invalid rollout zellij session name: $session_name" >&2
  exit 2
fi
session_slug="session-${session_name#hr-}"
job_runtime_dir="${RL_JOB_RUNTIME_ROOT}/${session_slug}"
layout_file="${job_runtime_dir}/harbor-rollout-${session_slug}.kdl"
lock_file="${RL_JOB_RUNTIME_ROOT}/${session_slug}.lock"

mkdir -p "$job_queue_dir/pending" "$job_queue_dir/active" "$job_queue_dir/results" "$job_runtime_dir" "$RL_JOB_RUNTIME_ROOT"

session_exists() {
  zellij list-sessions --short 2>/dev/null | grep -qx "$session_name"
}

session_ready() {
  session_exists && [[ -s "$layout_file" ]]
}

(
  if ! flock -x -w "${RL_JOB_ZELLIJ_LOCK_TIMEOUT:-10}" 9; then
    if session_ready; then
      printf '%s\n' "$session_name"
      exit 0
    fi
    echo "timed out waiting for submission zellij lock: $lock_file" >&2
    exit 1
  fi

  if session_ready; then
    printf '%s\n' "$session_name"
    exit 0
  fi

  ensure_zellij_web_sharing_config

  env \
    RL_ZELLIJ_ROLE=job \
    RL_ZELLIJ_SUBMISSION_ID="$ray_submission_id" \
    RL_QUEUE_DIR="$job_queue_dir" \
    RL_ACTIVE_DIR="$job_queue_dir/active" \
    RUNTIME_DIR="$job_runtime_dir" \
    LAYOUT_FILE="$layout_file" \
    JOBS_ROOT="${OUTPUT_PATH}/jobs/${agent_slug}/${submission_slug}" \
    "$RL_SCRIPT_DIR/gen_rl_rollout_zellij_layout.sh" "$layout_file" >/dev/null

  # If a previous cleanup removed the runtime layout but zellij still lists the
  # session, recreate it instead of returning an empty shell.
  zellij kill-session "$session_name" >/dev/null 2>&1 || true
  zellij delete-session "$session_name" >/dev/null 2>&1 || true
  zellij_cmd="$(printf 'stty rows 54 cols 172; exec zellij --session %q --new-session-with-layout %q' "$session_name" "$layout_file")"
  # Worker and monitor logs are already persisted. Do not duplicate every
  # terminal repaint into an unbounded zellij typescript file.
  nohup setsid env -u ZELLIJ_SESSION_NAME TERM=xterm-256color \
    RL_ZELLIJ_ROLE=job \
    RL_ZELLIJ_SUBMISSION_ID="$ray_submission_id" \
    RL_QUEUE_DIR="$job_queue_dir" \
    RL_ACTIVE_DIR="$job_queue_dir/active" \
    RUNTIME_DIR="$job_runtime_dir" \
    LAYOUT_FILE="$layout_file" \
    JOBS_ROOT="${OUTPUT_PATH}/jobs/${agent_slug}/${submission_slug}" \
    script -q -c "$zellij_cmd" /dev/null >/dev/null 2>&1 9>&- &

  # zellij can take tens of seconds to become visible under Docker-in-Docker
  # load.  Do not report failure while the detached session is still starting.
  ready_attempts=$(( (${RL_JOB_ZELLIJ_READY_TIMEOUT:-90} * 2) ))
  for _ in $(seq 1 "$ready_attempts"); do
    if session_ready; then
      printf '%s\n' "$session_name"
      exit 0
    fi
    sleep 0.5
  done

  echo "failed to create zellij session: $session_name" >&2
  exit 1
) 9>"$lock_file"
