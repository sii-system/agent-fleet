#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

case "${1:---dry-run}" in
  --dry-run) export HARBOR_DRY_RUN=1 ;;
  --execute) export HARBOR_DRY_RUN=0 ;;
  *) echo "Usage: bash run_e2b_smoke.sh [--dry-run|--execute]" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  echo "Usage: bash run_e2b_smoke.sh [--dry-run|--execute]" >&2
  exit 2
fi
if [[ "$HARBOR_DRY_RUN" == 0 && -z "${E2B_API_KEY:-}" ]]; then
  echo "[ERROR] set E2B_API_KEY in private host configuration" >&2
  exit 1
fi
if [[ "${E2B_DEBUG:-false}" == [tT][rR][uU][eE] ]]; then
  echo "[ERROR] unset E2B_DEBUG to exercise real sandbox creation and deletion" >&2
  exit 1
fi

# The canary deliberately selects the native backend, even on a host saved
# for another provider or the legacy single-template compatibility mode.
export AGENT=oracle ROLLOUT=0 RL_ENVIRONMENT_TYPE=e2b
export HARBOR_ENVIRONMENT_TYPE=e2b HARBOR_ENVIRONMENT_SPEC=e2b
export HARBOR_E2B_PREBUILT_TEMPLATE="" RL_E2B_PREBUILT_TEMPLATE="" E2B_TEMPLATE=""
export HARBOR_AGENT_IMPORT_PATH="" HARBOR_INCLUDE_TASKS=e2b-native FLEET_TASKS=""
export HARBOR_LIMIT=1 HARBOR_N_ATTEMPTS=1 HARBOR_N_CONCURRENT=1 HARBOR_MAX_RETRIES=0
export HARBOR_FORCE_BUILD=0 MIN_TEST=0 HARBOR_FIXER_VERIFICATION_RERUN=0
export OPIK_URL="" HARBOR_ANALYZER_ENABLED=0 HARBOR_MONITOR_ENABLED=0
export DATASET_NAME=auto DATASET_PATH="$REPO_ROOT/Tasks/Sandbox-smoke"
export HARBOR_E2B_SANDBOX_TIMEOUT_SEC="${HARBOR_E2B_SANDBOX_TIMEOUT_SEC:-${RL_E2B_SANDBOX_TIMEOUT_SEC:-600}}"
smoke_output="${OUTPUT_PATH:-$REPO_ROOT/runs/e2b-smoke-$(date +%Y%m%d-%H%M%S)-$$}"
mkdir -p "$(dirname "$smoke_output")"
mkdir "$smoke_output"
smoke_output="$(cd "$smoke_output" && pwd)"
export OUTPUT_PATH="$smoke_output" RUNTIME_DIR="$smoke_output/runtime"
export QUEUE_DIR="$smoke_output/queue" TASK_FILE="$smoke_output/tasks.txt"
mkdir -p "$RUNTIME_DIR" "$QUEUE_DIR"
source "$SCRIPT_DIR/env.sh"

for phase in initial repeat; do
  export JOBS_ROOT="$smoke_output/$phase"
  echo "[INFO] native E2B canary: $phase"
  bash "$SCRIPT_DIR/harboropik.sh"
  if [[ "$HARBOR_DRY_RUN" == 0 ]]; then
    "$HARBOR_OPIK_PYTHON" "$SCRIPT_DIR/e2b_smoke.py" "$JOBS_ROOT" --check-cleanup
  fi
done
echo "[INFO] canary output: $smoke_output"
