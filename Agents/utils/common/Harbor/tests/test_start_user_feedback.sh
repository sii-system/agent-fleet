#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP_DIR"' EXIT

FAKE_BIN="$TEST_TMP_DIR/bin"
mkdir -p "$FAKE_BIN" "$TEST_TMP_DIR/home"
cat > "$FAKE_BIN/zellij" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$OUTPUT_PATH" "$(dirname "$HARBOR_BENCHMARK_EXIT_FILE")"
cat > "$OUTPUT_PATH/summary.txt" <<EOF
status:      ${FAKE_SUMMARY_STATUS:-complete}
RUN_ID:      $RUN_ID
harbor_exit_code: ${FAKE_BENCHMARK_STATUS:-0}
mean_reward: ${FAKE_MEAN_REWARD:-1.0}
EOF
if [[ "${FAKE_WRITE_EXIT:-1}" == "1" ]]; then
  printf '%s\n' "${FAKE_BENCHMARK_STATUS:-0}" > "$HARBOR_BENCHMARK_EXIT_FILE"
fi
printf 'Bye from Zellij!\n'
printf 'keep_on_failure=%s\n' "${HARBOR_ZELLIJ_KEEP_ON_FAILURE:-unset}"
exit "${FAKE_ZELLIJ_STATUS:-0}"
SH
chmod +x "$FAKE_BIN/zellij"

run_start() {
  local output="$1"
  local log="$2"
  shift 2

  rm -rf "$output"
  env -i \
    HOME="$TEST_TMP_DIR/home" \
    PATH="$FAKE_BIN:/usr/bin:/bin" \
    AGENT_FLEET_BIN_DIR="$FAKE_BIN" \
    AGENT_FLEET_PATHS_FILE="$TEST_TMP_DIR/missing-paths.env" \
    AGENT_FLEET_RUNTIME_DIR="$TEST_TMP_DIR/zellij-runtime" \
    ZELLIJ_CONFIG_FILE="$TEST_TMP_DIR/home/zellij.kdl" \
    RUN_ID="feedback-test" \
    ZELLIJ_SESSION_NAME="feedback-session" \
    OUTPUT_PATH="$output" \
    DATASET_NAME="terminalbench21" \
    AGENT="claude-code" \
    BASE_URL="https://gateway.example.invalid" \
    API_KEY="fake-key" \
    MODEL="fake-model" \
    TRACE_TO_OPIK="false" \
    HARBOR_MONITOR_ENABLED="0" \
    HARBOR_ONLINE_ANALYSIS="0" \
    "$@" \
    bash "$HARBOR_DIR/start.sh" > "$log" 2>&1
}

SUCCESS_OUTPUT="$TEST_TMP_DIR/success"
SUCCESS_LOG="$TEST_TMP_DIR/success.log"
run_start "$SUCCESS_OUTPUT" "$SUCCESS_LOG" \
  FAKE_BENCHMARK_STATUS=0 FAKE_MEAN_REWARD=1.0
grep -q '^\[RUN\] RUN_ID: feedback-test$' "$SUCCESS_LOG"
grep -q '^\[RUN\] Zellij session: feedback-session$' "$SUCCESS_LOG"
grep -Fq "[RUN] output: $SUCCESS_OUTPUT" "$SUCCESS_LOG"
grep -Fq "[RUN] summary: $SUCCESS_OUTPUT/summary.txt" "$SUCCESS_LOG"
grep -q '^mean_reward: 1.0$' "$SUCCESS_LOG"
grep -q '^keep_on_failure=0$' "$SUCCESS_LOG"

FAILURE_OUTPUT="$TEST_TMP_DIR/failure"
FAILURE_LOG="$TEST_TMP_DIR/failure.log"
status=0
run_start "$FAILURE_OUTPUT" "$FAILURE_LOG" \
  FAKE_BENCHMARK_STATUS=7 FAKE_SUMMARY_STATUS=failed FAKE_MEAN_REWARD=unavailable \
  || status="$?"
[[ "$status" -eq 7 ]]
grep -q '^status:      failed$' "$FAILURE_LOG"
grep -q '^harbor_exit_code: 7$' "$FAILURE_LOG"

MISSING_EXIT_OUTPUT="$TEST_TMP_DIR/missing-exit"
MISSING_EXIT_LOG="$TEST_TMP_DIR/missing-exit.log"
status=0
run_start "$MISSING_EXIT_OUTPUT" "$MISSING_EXIT_LOG" \
  FAKE_WRITE_EXIT=0 FAKE_ZELLIJ_STATUS=0 || status="$?"
[[ "$status" -eq 1 ]]
grep -q 'Zellij ended before Harbor recorded a completion status' "$MISSING_EXIT_LOG"

echo "ok"
