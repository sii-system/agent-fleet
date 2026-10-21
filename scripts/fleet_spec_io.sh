#!/usr/bin/env bash
# Sourced library for FleetSpec v1 I/O; not an entry point. It must not set
# global shell options or EXIT traps: both are process-wide and belong to the
# sourcing script. It resolves its own jq module directory, so callers owe it
# nothing beyond bash and (for load/write) jq.

FLEET_SPEC_IO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fleet_spec_is_option_shaped() {
  # Any dash-prefixed value is a mangled command line, not a filename: an
  # allowlist of known options would silently accept mistyped ones (--dryrn)
  # and turn an intended preview into a live run. A file literally named
  # like an option stays expressible as ./-name. Bare - falls through so the
  # output-path validation rejects it with its stdin-specific message.
  case "$1" in
    -) return 1 ;;
    -*) return 0 ;;
    *) return 1 ;;
  esac
}

fleet_spec_validate_output_path() {
  local output="$1" output_dir
  [[ -n "$output" ]] || return 0
  [[ "$output" != "-" ]] || {
    printf '[ERROR] --output requires a file path, not -\n' >&2
    return 2
  }
  output_dir="$(dirname -- "$output")"
  [[ -d "$output_dir" ]] || {
    printf '[ERROR] output directory does not exist: %s\n' "$output_dir" >&2
    return 2
  }
  [[ ! -d "$output" ]] || {
    printf '[ERROR] output path is a directory: %s\n' "$output" >&2
    return 2
  }
}

fleet_spec_require_jq() {
  command -v jq >/dev/null 2>&1 || {
    printf '[ERROR] jq is required for FleetSpec JSON\n' >&2
    return 1
  }
}

fleet_spec_load_many() {
  local source spec_json normalized
  local normalized_inputs=()
  [[ $# -gt 0 ]] || {
    printf '[ERROR] --spec requires at least one file path or -\n' >&2
    return 2
  }
  fleet_spec_require_jq || return

  if [[ $# -gt 1 ]]; then
    for source in "$@"; do
      if [[ "$source" == "-" ]]; then
        printf '[ERROR] --spec - cannot be combined with other spec inputs\n' >&2
        return 2
      fi
    done
  fi

  for source in "$@"; do
    if [[ "$source" == "-" ]]; then
      spec_json="$(cat)"
    elif [[ -f "$source" && -r "$source" ]]; then
      spec_json="$(cat -- "$source")"
    else
      printf '[ERROR] FleetSpec is not readable: %s\n' "$source" >&2
      return 2
    fi

    if ! normalized="$(jq -ces -L "$FLEET_SPEC_IO_DIR" '
      include "fleet_spec_validate";
      if length != 1 then error("expected one JSON value") else .[0] end
      | if type == "object" then [. | fleet_spec_v1]
        elif type == "array" and length > 0 then map(fleet_spec_v1)
        else error("expected a FleetSpec object or non-empty array") end
    ' <<<"$spec_json" 2>/dev/null)"; then
      printf '[ERROR] invalid FleetSpec v1: %s\n' "$source" >&2
      printf '[ERROR] expected one object or non-empty array of objects with schema_version=1, taskset, optional task/agent/workers, and no other fields\n' >&2
      return 2
    fi
    normalized_inputs[${#normalized_inputs[@]}]="$normalized"
  done

  FLEET_SPECS_JSON="$(printf '%s\n' "${normalized_inputs[@]}" | jq -cs 'add')"
  FLEET_SPEC_COUNT="$(jq -r 'length' <<<"$FLEET_SPECS_JSON")"
}

fleet_spec_from_taskset_args() {
  local taskset="$1" task="$2" agent="$3" workers="$4"
  fleet_spec_require_jq || return
  if ! FLEET_SPEC_JSON="$(jq -cen -L "$FLEET_SPEC_IO_DIR" \
    --arg taskset "$taskset" --arg task "$task" \
    --arg agent "$agent" --arg workers "$workers" '
    include "fleet_spec_validate";
    ({schema_version: 1, taskset: $taskset}
      + (if $task == "" then {} else {task: $task} end)
      + (if $agent == "" then {} else {agent: $agent} end)
      + (if $workers == "" then {} else {workers: ($workers | tonumber)} end))
    | fleet_spec_v1
  ' 2>/dev/null)"; then
    printf '[ERROR] taskset arguments do not form a valid FleetSpec v1\n' >&2
    return 2
  fi
}

fleet_spec_normalize_task_values() {
  local raw="" value
  fleet_spec_require_jq || return
  for value in "$@"; do
    if [[ -n "$raw" ]]; then
      raw+=","
    fi
    raw+="$value"
  done
  if ! FLEET_TASK="$(jq -cern -L "$FLEET_SPEC_IO_DIR" --arg task "$raw" '
    include "fleet_spec_validate";
    $task
    | if type == "string" and (test("[[:cntrl:]]") | not)
         and (fleet_task_v1 | length > 0)
      then fleet_task_v1
      else error("invalid task selection")
      end
  ' 2>/dev/null)"; then
    printf '[ERROR] --task must contain at least one comma-separated task name without control characters\n' >&2
    return 2
  fi
}

fleet_spec_write() {
  local output="$1" spec_json="$2" output_dir tmp_file
  [[ -n "$output" ]] || return 0
  fleet_spec_validate_output_path "$output" || return
  output_dir="$(dirname -- "$output")"
  tmp_file="$(mktemp "$output_dir/.fleet-spec.XXXXXX")"
  # Clean up explicitly rather than via an EXIT trap: the EXIT slot is
  # process-global, so a library trap would clobber any trap the caller set.
  if ! jq . <<<"$spec_json" >"$tmp_file" || ! mv -f -- "$tmp_file" "$output"; then
    rm -f -- "$tmp_file"
    printf '[ERROR] failed to write FleetSpec: %s\n' "$output" >&2
    return 1
  fi
  printf '[INFO] FleetSpec written: %s\n' "$output" >&2
}
