#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HARBOR_DIR/../../../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

defaults="$(
  env \
    -u OUTPUT_ROOT \
    -u OUTPUT_PATH \
    -u OPIK_PROJECT_NAME \
    -u HARBOR_ZELLIJ_SESSION_NAME \
    HOME="$TEST_ROOT/home" \
    AGENT="opencode" \
    DATASET_NAME="terminalbench21" \
    MODEL="glm-5.2/fp8" \
    HARBOR_RUN_TIMESTAMP="20260723-120000" \
    HARBOR_SESSION_TIMESTAMP="120000" \
    AGENT_FLEET_PATHS_FILE="$TEST_ROOT/missing-paths.env" \
    AGENT_FLEET_RUNTIME_DIR="$TEST_ROOT/runtime" \
    bash -c '
      source "$1"
      printf "%s\n" \
        "$OUTPUT_ROOT" \
        "$OUTPUT_PATH" \
        "$OPIK_PROJECT_NAME" \
        "$HARBOR_ZELLIJ_SESSION_NAME"
    ' bash "$HARBOR_DIR/env.sh"
)"
default_output_root="$(printf '%s\n' "$defaults" | sed -n '1p')"
default_output_path="$(printf '%s\n' "$defaults" | sed -n '2p')"
default_project="$(printf '%s\n' "$defaults" | sed -n '3p')"
default_session="$(printf '%s\n' "$defaults" | sed -n '4p')"

[[ "$default_output_root" == "$REPO_ROOT/runs" ]]
[[ "$default_output_path" == "$REPO_ROOT/runs/"* ]]
[[ "$default_project" == \
  "agent-fleet-opencode-terminalbench21-glm-5-2-fp8-20260723-120000" ]]
[[ "$default_session" == \
  h-120000-*-opencode-terminal-glm-5* ]]
[[ "${#default_session}" -le 40 ]]
[[ "$default_session" != *"/"* ]]

overrides="$(
  env \
    HOME="$TEST_ROOT/home" \
    OUTPUT_ROOT="$TEST_ROOT/custom-runs" \
    OPIK_PROJECT_NAME="existing-project" \
    HARBOR_ZELLIJ_SESSION_NAME="existing-session" \
    AGENT_FLEET_PATHS_FILE="$TEST_ROOT/missing-paths.env" \
    AGENT_FLEET_RUNTIME_DIR="$TEST_ROOT/runtime" \
    bash -c '
      source "$1"
      printf "%s\n" \
        "$OUTPUT_ROOT" \
        "$OPIK_PROJECT_NAME" \
        "$HARBOR_ZELLIJ_SESSION_NAME"
    ' bash "$HARBOR_DIR/env.sh"
)"
override_output_root="$(printf '%s\n' "$overrides" | sed -n '1p')"
override_project="$(printf '%s\n' "$overrides" | sed -n '2p')"
override_session="$(printf '%s\n' "$overrides" | sed -n '3p')"

[[ "$override_output_root" == "$TEST_ROOT/custom-runs" ]]
[[ "$override_project" == "existing-project" ]]
[[ "$override_session" == "existing-session" ]]

echo "host default tests passed"
