#!/usr/bin/env bash
set -euo pipefail

MODEL_FUSION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_DIR="${HARBOR_DIR:-$(cd "$MODEL_FUSION_DIR/.." && pwd)}"
REPO_ROOT="$(cd "$MODEL_FUSION_DIR/../../../../.." && pwd)"
_USER_SUBAGENT_MODEL_SET="${TB_CLAUDE_CODE_SUBAGENT_MODEL+x}"
_USER_SUBAGENT_MODEL_VALUE="${TB_CLAUDE_CODE_SUBAGENT_MODEL-}"
_USER_ANTHROPIC_MODEL_SET="${TB_ANTHROPIC_MODEL+x}"
_USER_ANTHROPIC_MODEL_VALUE="${TB_ANTHROPIC_MODEL-}"
_USER_ANTHROPIC_OPUS_MODEL_SET="${TB_ANTHROPIC_DEFAULT_OPUS_MODEL+x}"
_USER_ANTHROPIC_OPUS_MODEL_VALUE="${TB_ANTHROPIC_DEFAULT_OPUS_MODEL-}"
_USER_ANTHROPIC_SONNET_MODEL_SET="${TB_ANTHROPIC_DEFAULT_SONNET_MODEL+x}"
_USER_ANTHROPIC_SONNET_MODEL_VALUE="${TB_ANTHROPIC_DEFAULT_SONNET_MODEL-}"
_USER_ANTHROPIC_HAIKU_MODEL_SET="${TB_ANTHROPIC_DEFAULT_HAIKU_MODEL+x}"
_USER_ANTHROPIC_HAIKU_MODEL_VALUE="${TB_ANTHROPIC_DEFAULT_HAIKU_MODEL-}"

TASK_ID="${TASK_ID:-fix-git}"
task_slug="$(printf '%s' "$TASK_ID" | tr -c '[:alnum:]_.-' '_')"
if [[ -z "$task_slug" || "$task_slug" == "." || "$task_slug" == ".." ]]; then
  task_slug="task"
fi
RUN_ID="${RUN_ID:-$(date +%Y-%m-%d-%H%M%S)-tb21-mid-turn-fusion-${task_slug}-$$}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/tmp/harbor-mid-turn-runs}"
OUTPUT_PATH="${OUTPUT_PATH:-${OUTPUT_ROOT}/${RUN_ID}}"
FUSION_ROUTER_DIR="${FUSION_ROUTER_DIR:-$(cd "$REPO_ROOT/.." && pwd)/sii-fusion-router}"
SPAN_FORCE_MODE="${SPAN_FORCE_MODE:-mid-turn-fusion}"
MID_TURN_PREPARE_ONLY="${MID_TURN_PREPARE_ONLY:-0}"

if [[ "$SPAN_FORCE_MODE" != "mid-turn-fusion" ]]; then
  echo "[ERROR] only SPAN_FORCE_MODE=mid-turn-fusion is supported; got: $SPAN_FORCE_MODE" >&2
  exit 2
fi

if [[ -z "${DATASET_PATH:-}" ]]; then
  for candidate in \
    /workspace/terminal-bench-2-verified \
    /workspace/terminal-bench-2-1/tasks; do
    if [[ -d "$candidate/$TASK_ID" ]]; then
      DATASET_PATH="$candidate"
      break
    fi
  done
fi
: "${DATASET_PATH:?set DATASET_PATH to a Terminal-Bench task directory root}"

export RUN_ID OUTPUT_ROOT OUTPUT_PATH FUSION_ROUTER_DIR
export DATASET_NAME="${DATASET_NAME:-auto}"
export DATASET_PATH
export TASK_SOURCE_FILE="${TASK_SOURCE_FILE:-${OUTPUT_PATH}/mid-turn-task-source.txt}"
export TOTAL_WORKERS="${TOTAL_WORKERS:-1}"
export TB_N_CONCURRENT="${TB_N_CONCURRENT:-1}"
export N_ATTEMPTS="${N_ATTEMPTS:-1}"
export TB_RUNS="${TB_RUNS:-1}"
export MAX_RETRIES="${MAX_RETRIES:-0}"
export TB_MAX_RETRIES="${TB_MAX_RETRIES:-0}"
export AGENT="claude-code"
export INCLUDE_TASKS="$TASK_ID"
export TB_INCLUDE_TASKS="$TASK_ID"
export TB_TASK_ID="$TASK_ID"
export TB_LIMIT=""
export OPIK_PROJECT_NAME="${OPIK_PROJECT_NAME:-tb21-mid-turn-${TASK_ID}-${RUN_ID}}"

# shellcheck source=/dev/null
. "$HARBOR_DIR/env.sh"

# Keep every integration override local to this wrapper. The shared Harbor
# scripts remain untouched: this process swaps in a scoped Opik CLI proxy and
# a scoped sitecustomize overlay, and child workers inherit only these exports.
MODEL_FUSION_REAL_HARBOR_OPIK_BIN="${MODEL_FUSION_REAL_HARBOR_OPIK_BIN:-$HARBOR_OPIK_BIN}"
HARBOR_OPIK_BIN="$MODEL_FUSION_DIR/harboropik.sh"
HARBOR_CLAUDE_CODE_DIR="$MODEL_FUSION_DIR"
export MODEL_FUSION_REAL_HARBOR_OPIK_BIN HARBOR_OPIK_BIN HARBOR_CLAUDE_CODE_DIR

# shellcheck source=/dev/null
. "$MODEL_FUSION_DIR/env.sh"

# Preserve the wrapper's explicit empty-subagent-model behavior after the
# shared Harbor defaults have been loaded.
if [[ "$_USER_SUBAGENT_MODEL_SET" == "x" ]]; then
  TB_CLAUDE_CODE_SUBAGENT_MODEL="$_USER_SUBAGENT_MODEL_VALUE"
else
  TB_CLAUDE_CODE_SUBAGENT_MODEL=""
fi

MAIN_MODEL="${MAIN_MODEL:-$MODEL}"
SPAN_PANEL_MODELS="${SPAN_PANEL_MODELS:-minimax2.7,minimax2.7}"
SPAN_OUTER_MODEL="${SPAN_OUTER_MODEL:-minimax2.7}"
SPAN_ARTIFACT_DIR="${SPAN_ARTIFACT_DIR:-${OUTPUT_PATH}/mid-turn-fusion/${task_slug}}"
SPAN_FUSION_JSON_COPY="${SPAN_FUSION_JSON_COPY:-${OUTPUT_PATH}/${task_slug}.fusion.json}"
SPAN_TASK_SUBAGENT_PROMPT_FILE="${SPAN_TASK_SUBAGENT_PROMPT_FILE:-${SPAN_ARTIFACT_DIR}/task-subagent-system-prompt.md}"
SPAN_TASK_SUBAGENT_AGENTS_FILE="${SPAN_TASK_SUBAGENT_AGENTS_FILE:-${SPAN_ARTIFACT_DIR}/claude-agents.json}"

export MODEL="$MAIN_MODEL"
export TB_MODEL="$MAIN_MODEL"
if [[ "$_USER_ANTHROPIC_MODEL_SET" == "x" ]]; then
  TB_ANTHROPIC_MODEL="$_USER_ANTHROPIC_MODEL_VALUE"
else
  TB_ANTHROPIC_MODEL="$MAIN_MODEL"
fi
if [[ "$_USER_ANTHROPIC_OPUS_MODEL_SET" == "x" ]]; then
  TB_ANTHROPIC_DEFAULT_OPUS_MODEL="$_USER_ANTHROPIC_OPUS_MODEL_VALUE"
else
  TB_ANTHROPIC_DEFAULT_OPUS_MODEL="$MAIN_MODEL"
fi
if [[ "$_USER_ANTHROPIC_SONNET_MODEL_SET" == "x" ]]; then
  TB_ANTHROPIC_DEFAULT_SONNET_MODEL="$_USER_ANTHROPIC_SONNET_MODEL_VALUE"
else
  TB_ANTHROPIC_DEFAULT_SONNET_MODEL="$MAIN_MODEL"
fi
if [[ "$_USER_ANTHROPIC_HAIKU_MODEL_SET" == "x" ]]; then
  TB_ANTHROPIC_DEFAULT_HAIKU_MODEL="$_USER_ANTHROPIC_HAIKU_MODEL_VALUE"
else
  TB_ANTHROPIC_DEFAULT_HAIKU_MODEL="$MAIN_MODEL"
fi
export TB_ANTHROPIC_MODEL
export TB_ANTHROPIC_DEFAULT_OPUS_MODEL
export TB_ANTHROPIC_DEFAULT_SONNET_MODEL
export TB_ANTHROPIC_DEFAULT_HAIKU_MODEL
export TB_CLAUDE_CODE_SUBAGENT_MODEL

# Keep this wrapper's disposable job state out of the shared Harbor jobs root.
# A reused OUTPUT_PATH may contain results from an older task; scoping and
# recreating this directory prevents finalize from selecting those results.
JOBS_ROOT="${JOBS_ROOT}/model-fusion-${task_slug}"
export JOBS_ROOT

export SPAN_FORCE_MODE=mid-turn-fusion
export SPAN_FORCE_FUSION=1
export SPAN_PANEL_MODELS

panel_count=0
IFS=',' read -r -a panel_model_items <<< "$SPAN_PANEL_MODELS"
for panel_model in "${panel_model_items[@]}"; do
  panel_model="${panel_model//[[:space:]]/}"
  if [[ -n "$panel_model" ]]; then
    panel_count=$((panel_count + 1))
  fi
done
if [[ "$panel_count" -lt 1 ]]; then
  echo "[ERROR] SPAN_PANEL_MODELS must contain at least one model" >&2
  exit 2
fi
export SPAN_PANEL_COUNT="$panel_count"

export TB_FUSION_ROUND_ROUTER_DIR="$FUSION_ROUTER_DIR/src/sii_fusion_router/frontends/claude_code"
export TB_FUSION_TASK_BUILDER="$TB_FUSION_ROUND_ROUTER_DIR/task_subagent_prompt.py"
export TB_FUSION_ROUND_ROUTER_MOUNT_PATH="/opt/tb-fusion-round"
export TB_FUSION_ROUND_GATE=1
export TB_FUSION_ROUND_GATE_PATH="/opt/tb-fusion-round/subagent_barrier_gate.py"
export TB_FUSION_ROUND_GATE_MODE=mid-turn-fusion
export TB_FUSION_MAX_FUSIONS_PER_TASK="${TB_FUSION_MAX_FUSIONS_PER_TASK:-1}"
export TB_FUSION_PANEL_CALL_BUDGET="${TB_FUSION_PANEL_CALL_BUDGET:-}"
export SPAN_GATE_STATE_PATH="${SPAN_GATE_STATE_PATH:-/logs/agent/.gate-state.json}"
export SPAN_MID_TURN_ARTIFACT_ROOT="${SPAN_MID_TURN_ARTIFACT_ROOT:-/logs/agent/span-mid-turn-artifacts}"

for required_path in \
  "$FUSION_ROUTER_DIR" \
  "$TB_FUSION_ROUND_ROUTER_DIR" \
  "$TB_FUSION_TASK_BUILDER" \
  "$FUSION_ROUTER_DIR/prompts/mid_turn_fusion/panel.md" \
  "$FUSION_ROUTER_DIR/prompts/mid_turn_fusion/outer.md" \
  "$TB_FUSION_ROUND_ROUTER_DIR/subagent_barrier_gate.py" \
  "$TB_FUSION_ROUND_ROUTER_DIR/templates"; do
  if [[ ! -e "$required_path" ]]; then
    echo "[ERROR] required Router checkout artifact is missing: $required_path" >&2
    echo "[ERROR] set FUSION_ROUTER_DIR to a built sii-fusion-router checkout containing the Claude Code frontend" >&2
    exit 2
  fi
done

find_task_instruction_file() {
  local task_name="$1"
  local candidate
  if [[ -d "$DATASET_PATH/$task_name" ]]; then
    for candidate in task.yaml task.yml task.md prompt.md instruction.md README.md task.toml; do
      if [[ -f "$DATASET_PATH/$task_name/$candidate" ]]; then
        printf '%s\n' "$DATASET_PATH/$task_name/$candidate"
        return 0
      fi
    done
  fi
  echo "unable to locate task instruction for $task_name under $DATASET_PATH" >&2
  return 1
}

mkdir -p "$OUTPUT_PATH" "$SPAN_ARTIFACT_DIR"
printf '%s\n' "$TASK_ID" > "$TASK_SOURCE_FILE"
task_file="$(find_task_instruction_file "$TASK_ID")"
export TB_FUSION_TASK_FILE_SOURCE="$task_file"
export TB_FUSION_TASK_FILE="/opt/tb-fusion-task/$(basename "$task_file")"

python3 "$TB_FUSION_TASK_BUILDER" prepare \
  --task-id "$TASK_ID" \
  --task-file "$task_file" \
  --context-dir "$DATASET_PATH/$TASK_ID" \
  --artifact-dir "$SPAN_ARTIFACT_DIR" \
  --panel-models "$SPAN_PANEL_MODELS" \
  --outer-model "$SPAN_OUTER_MODEL" \
  --main-model "$MAIN_MODEL" \
  --output-prompt "$SPAN_TASK_SUBAGENT_PROMPT_FILE" \
  --output-agents "$SPAN_TASK_SUBAGENT_AGENTS_FILE" \
  --output-fusion "$SPAN_ARTIFACT_DIR/fusion.json" \
  --force-mode mid-turn-fusion \
  --force-fusion

export TB_CLAUDE_CODE_AGENTS_JSON="$(tr -d '\n' < "$SPAN_TASK_SUBAGENT_AGENTS_FILE")"

filtered_disallowed=""
for tool in ${TB_DISALLOWED_TOOLS//,/ }; do
  if [[ "$tool" == "Task" || "$tool" == "Agent" || "$tool" == "Bash" ]]; then
    continue
  fi
  filtered_disallowed="${filtered_disallowed:+$filtered_disallowed }$tool"
done
export TB_DISALLOWED_TOOLS="$filtered_disallowed"

task_subagent_prompt="$(<"$SPAN_TASK_SUBAGENT_PROMPT_FILE")"
export TB_APPEND_SYSTEM_PROMPT="${TB_APPEND_SYSTEM_PROMPT:-}

${task_subagent_prompt}"

if [[ "$MID_TURN_PREPARE_ONLY" == "1" ]]; then
  printf 'prepared mid-turn fusion contract at %s\n' "$SPAN_ARTIFACT_DIR"
  exit 0
fi

export RESET_RUN=1
harbor_init_run_dirs
harbor_reset_run_state
rm -rf -- "$JOBS_ROOT"
harbor_init_run_dirs
harbor_ensure_dataset
harbor_prepare_task_file
harbor_prepare_or_select_wheels
bash "$HARBOR_DIR/run_harbor_worker.sh" 1

result_json="$JOBS_ROOT/worker-1/1-${task_slug}/result.json"
if [[ ! -f "$result_json" ]]; then
  result_json=""
fi
python3 "$TB_FUSION_TASK_BUILDER" finalize \
  --task-id "$TASK_ID" \
  --fusion-json "$SPAN_ARTIFACT_DIR/fusion.json" \
  --jobs-root "$JOBS_ROOT" \
  --result-json "$result_json"
cp "$SPAN_ARTIFACT_DIR/fusion.json" "$SPAN_FUSION_JSON_COPY"

printf 'fusion_json=%s\n' "$SPAN_FUSION_JSON_COPY"
printf 'output_path=%s\n' "$OUTPUT_PATH"
