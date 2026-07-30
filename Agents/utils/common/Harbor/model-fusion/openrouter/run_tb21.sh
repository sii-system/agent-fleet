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
task-list, worker, and trial settings. Full mode requires TASK_SOURCE_FILE.
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
[[ -d "$FUSION_ROUTER_DIR/.git" ]] || die "Router checkout not found: $FUSION_ROUTER_DIR"
command -v python3 >/dev/null || die "python3 is required"
command -v uv >/dev/null || die "uv is required"

stamp="$(date +%Y%m%d-%H%M%S)"
RUN_ID="${RUN_ID:-openrouter-${stamp}-$$}"
OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-$RUN_ID}"
export RUN_ID OPIK_PROJECT_NAME
# shellcheck source=/dev/null
. "$HARBOR_DIR/env.sh"

router_version="$(PYTHONPATH="$FUSION_ROUTER_DIR/src" python3 -c 'import sii_fusion_router; print(sii_fusion_router.__version__)')"
router_commit="$(git -C "$FUSION_ROUTER_DIR" rev-parse HEAD)"
router_short="${router_commit:0:12}"
dist_dir="${OPENROUTER_DIST_DIR:-${OUTPUT_ROOT}/.openrouter/$router_version-$router_short}"
mkdir -p "$dist_dir"
OPENROUTER_WHEEL="$dist_dir/sii_fusion_router-${router_version}-py3-none-any.whl"
[[ -f "$OPENROUTER_WHEEL" ]] || uv build --wheel --out-dir "$dist_dir" "$FUSION_ROUTER_DIR"
[[ -f "$OPENROUTER_WHEEL" ]] || die "Router wheel was not produced"

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
OPENROUTER_CONFIG="$dist_dir/router-config-openrouter.json"
python3 - "$source_config" "$OPENROUTER_CONFIG" "${OPENROUTER_MAX_FUSIONS:--1}" <<'PY'
import json
import sys
from pathlib import Path

source, target, max_fusions = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
if max_fusions < -1:
    raise SystemExit("OPENROUTER_MAX_FUSIONS must be -1 or greater")
payload = json.loads(source.read_text(encoding="utf-8"))
payload.setdefault("routing", {})["max_fusions"] = max_fusions
models = payload.setdefault("models", {})
models.update({
    "panels": ["sonnet", "sonnet"],
    "reviewer": "sonnet",
    "outer": "sonnet",
    "spec_checklist": "sonnet",
})
target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

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
printf '[openrouter] router=%s@%s max_fusions=%s\n' \
  "$router_version" "$router_short" "${OPENROUTER_MAX_FUSIONS:--1}"
[[ "$MODE" != "build" && "$MODE" != "doctor" ]] || exit 0

[[ -n "${BASE_URL:-}" ]] || die "BASE_URL is required"
[[ -n "${API_KEY:-}" && "${API_KEY:-}" != "xxx" ]] || die "API_KEY is required"
[[ -d "$DATASET_PATH" ]] || die "DATASET_PATH not found: $DATASET_PATH"

export OPENROUTER_DIR FUSION_ROUTER_DIR OPENROUTER_WHEEL
export OPENROUTER_VERSION="$router_version" OPENROUTER_CONFIG
# shellcheck source=/dev/null
. "$OPENROUTER_DIR/env.sh"
OPENROUTER_REAL_HARBOR_OPIK_BIN="$HARBOR_OPIK_BIN"
HARBOR_OPIK_BIN="$OPENROUTER_DIR/harboropik.sh"
HARBOR_CLAUDE_CODE_DIR="$OPENROUTER_DIR"
export OPENROUTER_REAL_HARBOR_OPIK_BIN HARBOR_OPIK_BIN HARBOR_CLAUDE_CODE_DIR

export AGENT=claude-code DATASET_NAME="${DATASET_NAME:-auto}"
export TB_AGENT_TIMEOUT_MULTIPLIER="${TB_AGENT_TIMEOUT_MULTIPLIER:-20}"
export TB_ANTHROPIC_MODEL="${TB_ANTHROPIC_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_OPUS_MODEL="${TB_ANTHROPIC_DEFAULT_OPUS_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_SONNET_MODEL="${TB_ANTHROPIC_DEFAULT_SONNET_MODEL:-$MODEL}"
export TB_ANTHROPIC_DEFAULT_HAIKU_MODEL="${TB_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$MODEL}"
export TB_CLAUDE_CODE_SUBAGENT_MODEL="${TB_CLAUDE_CODE_SUBAGENT_MODEL:-$MODEL}"

if [[ "$MODE" == "dry-run" ]]; then
  export TB_DRY_RUN=1 TB_MIN_TEST=1 TB_MIN_TEST_INCLUDE_TASK="$TASK_ID"
  export INCLUDE_TASKS="$TASK_ID" TB_INCLUDE_TASKS="$TASK_ID"
  (cd "$HARBOR_DIR" && bash harboropik.sh)
  exit 0
fi
export TB_DRY_RUN=0 TB_MIN_TEST=0
if [[ "$MODE" == "smoke" ]]; then
  task_file="$(mktemp "${TMPDIR:-/tmp}/openrouter-task.XXXXXX")"
  printf '%s\n' "$TASK_ID" > "$task_file"
  export TASK_SOURCE_FILE="$task_file"
  export TOTAL_WORKERS=1 TB_N_CONCURRENT=1 N_ATTEMPTS=1 TB_RUNS=1
  export MAX_RETRIES=0 TB_MAX_RETRIES=0
else
  [[ -n "${TASK_SOURCE_FILE:-}" && -f "$TASK_SOURCE_FILE" ]] || die "full mode requires TASK_SOURCE_FILE"
  export TB_RUNS="${TB_RUNS:-${N_ATTEMPTS:-1}}"
  export TB_MAX_RETRIES="${TB_MAX_RETRIES:-${MAX_RETRIES:-0}}"
fi
start_args=()
[[ "${DETACH:-1}" != "1" ]] || start_args+=(--detach)
(cd "$HARBOR_DIR" && bash start.sh "${start_args[@]}")
printf '[openrouter] run_id=%s\n[openrouter] output_path=%s\n' "$RUN_ID" "$OUTPUT_PATH"
