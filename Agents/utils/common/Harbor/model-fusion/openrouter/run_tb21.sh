#!/usr/bin/env bash
set -euo pipefail

OPENROUTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_DIR="${HARBOR_DIR:-$(cd "$OPENROUTER_DIR/../.." && pwd)}"
REPO_ROOT="$(cd "$OPENROUTER_DIR/../../../../../.." && pwd)"
MODE="${1:-}"
TASK_ID="${2:-${TASK_ID:-configure-git-webserver}}"

usage() {
  cat <<'EOF'
Run Fusion Router's openrouter_fusion pipeline through Agent Fleet.

Usage:
  run_tb21.sh build
  run_tb21.sh doctor
  run_tb21.sh dry-run [task-id]
  run_tb21.sh smoke [task-id]
  run_tb21.sh full

The caller or config.local.env owns API_KEY, BASE_URL, Opik, dataset, model,
worker, and trial settings. Full mode defaults to the complete TB 2.1 task list.
OPENROUTER_MAX_FUSIONS defaults to -1 (unlimited).
EOF
}

die() { echo "[openrouter] ERROR: $*" >&2; exit 2; }
case "$MODE" in
  build|doctor|dry-run|smoke|full) ;;
  help|-h|--help|"") usage; [[ -n "$MODE" ]] || exit 2; exit 0 ;;
  *) usage >&2; die "unknown mode: $MODE" ;;
esac

FUSION_ROUTER_DIR="${FUSION_ROUTER_DIR:-$(cd "$REPO_ROOT/.." && pwd)/sii-fusion-router}"
[[ "$(git -C "$FUSION_ROUTER_DIR" rev-parse --is-inside-work-tree 2>/dev/null || true)" == "true" ]] \
  || die "Router git checkout not found: $FUSION_ROUTER_DIR"
command -v python3 >/dev/null || die "python3 is required"
command -v uv >/dev/null || die "uv is required"

stamp="$(date +%Y%m%d-%H%M%S)"
RUN_ID="${RUN_ID:-openrouter-${stamp}-$$}"
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-$RUN_ID}"
AGENT=claude-code
export RUN_ID OPIK_PROJECT_NAME AGENT
# shellcheck source=/dev/null
. "$HARBOR_DIR/env.sh"

router_version="$(PYTHONPATH="$FUSION_ROUTER_DIR/src" python3 -c 'import sii_fusion_router; print(sii_fusion_router.__version__)')"
router_commit="$(git -C "$FUSION_ROUTER_DIR" rev-parse HEAD)"
router_short="${router_commit:0:12}"
wheel_metadata="$(
  python3 "$OPENROUTER_DIR/../router_cli_utils.py" build-wheel \
    --repo "$FUSION_ROUTER_DIR" \
    --cache-root "${OPENROUTER_DIST_DIR:-${OUTPUT_ROOT}/.openrouter}" \
    --version "$router_version"
)"
mapfile -t wheel_values < <(
  python3 - "$wheel_metadata" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
for key in ("cache_dir", "source_hash", "wheel", "wheel_sha256"):
    print(payload[key])
PY
)
[[ "${#wheel_values[@]}" -eq 4 ]] || die "invalid Router wheel metadata"
dist_dir="${wheel_values[0]}"
router_source_hash="${wheel_values[1]}"
OPENROUTER_WHEEL="${wheel_values[2]}"
router_wheel_sha256="${wheel_values[3]}"
router_source_short="${router_source_hash:0:12}"

runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/openrouter-runtime.XXXXXX")"
task_file=""
cleanup() {
  rm -rf -- "$runtime_dir"
  [[ -z "$task_file" || ! -f "$task_file" ]] || rm -f -- "$task_file"
}
trap cleanup EXIT INT TERM
python3 -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
  "$OPENROUTER_WHEEL" "$runtime_dir"
actual_version="$(PYTHONPATH="$runtime_dir" python3 -m sii_fusion_router.cli --version)"
[[ "$actual_version" == "$router_version" ]] || die "wheel/source version mismatch"

source_config="${OPENROUTER_SOURCE_CONFIG:-$FUSION_ROUTER_DIR/examples/router-config.json}"
[[ -f "$source_config" ]] || die "Router config not found: $source_config"
OPENROUTER_CONFIG="$(
  python3 "$OPENROUTER_DIR/../router_cli_utils.py" derive-config \
    --source "$source_config" \
    --output-dir "$dist_dir" \
    --pipeline openrouter_fusion \
    --max-fusions "${OPENROUTER_MAX_FUSIONS:--1}"
)"

doctor_json="$(
  ANTHROPIC_BASE_URL="${BASE_URL:-http://127.0.0.1:9}" \
    PYTHONPATH="$runtime_dir" python3 -m sii_fusion_router.cli doctor \
      --pipeline openrouter_fusion --config "$OPENROUTER_CONFIG" --claude-bin true
)"
python3 - "$doctor_json" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
if d.get("status") != "ok" or d.get("pipeline") != "openrouter_fusion":
    raise SystemExit(f"Router doctor failed: {d}")
PY

printf '[openrouter] wheel=%s\n' "$OPENROUTER_WHEEL"
printf '[openrouter] config=%s\n' "$OPENROUTER_CONFIG"
printf '[openrouter] router=%s@%s source=%s max_fusions=%s\n' \
  "$router_version" "$router_short" "$router_source_short" \
  "${OPENROUTER_MAX_FUSIONS:--1}"
printf '[openrouter] wheel_sha256=%s\n' "$router_wheel_sha256"
[[ "$MODE" != "build" && "$MODE" != "doctor" ]] || exit 0

[[ -n "${BASE_URL:-}" ]] || die "BASE_URL is required"
[[ -n "${API_KEY:-}" && "${API_KEY:-}" != "xxx" ]] || die "API_KEY is required"
harbor_uses_registry_dataset || [[ -d "$DATASET_PATH" ]] \
  || die "DATASET_PATH not found: $DATASET_PATH"

export OPENROUTER_DIR FUSION_ROUTER_DIR OPENROUTER_WHEEL
export OPENROUTER_VERSION="$router_version" OPENROUTER_CONFIG
# shellcheck source=/dev/null
. "$OPENROUTER_DIR/env.sh"
OPENROUTER_REAL_HARBOR_OPIK_BIN="$HARBOR_OPIK_BIN"
HARBOR_OPIK_BIN="$OPENROUTER_DIR/harboropik.sh"
HARBOR_CLAUDE_CODE_DIR="$OPENROUTER_DIR"
export OPENROUTER_REAL_HARBOR_OPIK_BIN HARBOR_OPIK_BIN HARBOR_CLAUDE_CODE_DIR

export TB_AGENT_TIMEOUT_MULTIPLIER="${TB_AGENT_TIMEOUT_MULTIPLIER:-20}"
export TB_ANTHROPIC_MODEL="${TB_ANTHROPIC_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_OPUS_MODEL="${TB_ANTHROPIC_DEFAULT_OPUS_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_SONNET_MODEL="${TB_ANTHROPIC_DEFAULT_SONNET_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_HAIKU_MODEL="${TB_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$MODEL}"
export TB_CLAUDE_CODE_SUBAGENT_MODEL="${TB_CLAUDE_CODE_SUBAGENT_MODEL:-$MODEL}"

if [[ "$MODE" == "dry-run" ]]; then
  export TB_DRY_RUN=1 MIN_TEST=1 MIN_TEST_INCLUDE_TASK="$TASK_ID"
  export INCLUDE_TASKS="$TASK_ID" TB_INCLUDE_TASKS="$TASK_ID"
  mkdir -p "$OUTPUT_PATH" "$RUNTIME_DIR"
  proxy_args=(harbor run)
  if harbor_uses_registry_dataset; then
    proxy_args+=(--dataset "$(harbor_registry_dataset_name)")
  else
    proxy_args+=(--path "$DATASET_PATH")
  fi
  proxy_args+=(-i "$(harbor_registry_task_name "$TASK_ID")")
  MODEL_FUSION_PROXY_RENDER_ONLY=1 "$HARBOR_OPIK_BIN" "${proxy_args[@]}"
  exit 0
fi
export TB_DRY_RUN=0 MIN_TEST=0
if [[ "$MODE" == "smoke" ]]; then
  task_file="$(mktemp "${TMPDIR:-/tmp}/openrouter-task.XXXXXX")"
  printf '%s\n' "$TASK_ID" > "$task_file"
  export TASK_SOURCE_FILE="$task_file"
  export INCLUDE_TASKS="$TASK_ID" TB_INCLUDE_TASKS="$TASK_ID"
  export TOTAL_WORKERS=1 TB_N_CONCURRENT=1 N_ATTEMPTS=1 TB_RUNS=1
  export MAX_RETRIES=0 TB_MAX_RETRIES=0
else
  TASK_SOURCE_FILE="${TASK_SOURCE_FILE:-$REPO_ROOT/Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt}"
  [[ -s "$TASK_SOURCE_FILE" ]] || die "task list not found or empty: $TASK_SOURCE_FILE"
  INCLUDE_TASKS="$(python3 - "$TASK_SOURCE_FILE" <<'PY'
import sys
from pathlib import Path

tasks = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines()]
tasks = [task for task in tasks if task and not task.startswith("#")]
if not tasks or any("," in task for task in tasks):
    raise SystemExit("task list must contain nonempty task IDs without commas")
print(",".join(tasks))
PY
)"
  export TASK_SOURCE_FILE INCLUDE_TASKS TB_INCLUDE_TASKS="$INCLUDE_TASKS"
  export TB_RUNS="${TB_RUNS:-${N_ATTEMPTS:-1}}"
  export TB_MAX_RETRIES="${TB_MAX_RETRIES:-${MAX_RETRIES:-0}}"
fi
start_args=()
[[ "${DETACH:-1}" != "1" ]] || start_args+=(--detach)
(cd "$HARBOR_DIR" && bash start.sh "${start_args[@]}")
printf '[openrouter] run_id=%s\n[openrouter] output_path=%s\n' "$RUN_ID" "$OUTPUT_PATH"
