#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_TMP_DIR=""

make_fake_bin() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"

  cat >"$fake_bin/docker" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "info" ]]; then
  exit 0
fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  exit 1
fi
exit 0
SH

  cat >"$fake_bin/curl" <<'SH'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == "%{http_code}" ]]; then
    printf '200'
  fi
done
exit 0
SH

  cat >"$fake_bin/git" <<'SH'
#!/usr/bin/env bash
exit 0
SH

  cat >"$fake_bin/uv" <<'SH'
#!/usr/bin/env bash
exit 0
SH

  cat >"$fake_bin/uvx" <<'SH'
#!/usr/bin/env bash
exit 0
SH

  cat >"$fake_bin/file" <<'SH'
#!/usr/bin/env bash
printf 'ELF 64-bit LSB executable\n'
SH

  chmod +x \
    "$fake_bin"/docker \
    "$fake_bin"/curl \
    "$fake_bin"/file \
    "$fake_bin"/git \
    "$fake_bin"/uv \
    "$fake_bin"/uvx
}

make_capture_bin() {
  local path="$1"
  cat >"$path" <<'SH'
#!/usr/bin/env bash
printf '%s' "${OPIK_TRACK_DISABLE:-}" >"${HARBOR_CAPTURE_FILE}.opik-track-disable"
python3 - "$HARBOR_CAPTURE_FILE" "$@" <<'PY'
import json
import os
import sys
from pathlib import Path

capture = Path(sys.argv[1])
args = sys.argv[2:]
capture.parent.mkdir(parents=True, exist_ok=True)
capture.write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
connection_fields = (
    "OPIK_URL",
    "OPIK_URL_OVERRIDE",
    "OPIK_BASE",
    "OPIK_PROJECT_NAME",
    "OPIK_API_KEY",
    "OPIK_WORKSPACE",
)
inherited = {name: os.environ[name] for name in connection_fields if name in os.environ}
Path(f"{capture}.opik-environment").write_text(json.dumps(inherited, sort_keys=True))

if "--mounts-json" in args:
    mounts = json.loads(args[args.index("--mounts-json") + 1])
    verifier_mount = next(
        (
            mount
            for mount in mounts
            if mount.get("target") == "/opt/tb-uv-backup/bin"
        ),
        None,
    )
    if verifier_mount:
        source = Path(verifier_mount["source"])
        names = sorted(path.name for path in source.iterdir())
        Path(f"{capture}.verifier-tools").write_text(",".join(names))

if os.environ.get("HARBOR_CAPTURE_RESULT") == "1":
    pid_file = Path(os.environ["HARBOR_BENCHMARK_PID_FILE"])
    if pid_file.is_file():
        recorded_pid, recorded_start_time = pid_file.read_text().split()
        actual_start_time = Path(f"/proc/{recorded_pid}/stat").read_text().split()[21]
        if recorded_start_time != actual_start_time:
            raise SystemExit(
                "benchmark PID identity mismatch: "
                f"pid={recorded_pid} recorded={recorded_start_time} actual={actual_start_time}"
            )
        Path(f"{capture}.pid-identity").write_text("valid")
    output = Path(args[args.index("-o") + 1]) / "fake-run"
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "finished_at": "2026-07-22T08:00:00Z",
        "n_total_trials": 2,
        "stats": {
            "n_completed_trials": 2,
            "n_errored_trials": 1,
            "n_cancelled_trials": 0,
            "n_retries": 1,
            "evals": {
                "fake-eval": {
                    "n_trials": 2,
                    "n_errors": 1,
                    "metrics": [{"mean": 0.5}],
                    "reward_stats": {"reward": {"1.0": ["trial-1"]}},
                    "exception_stats": {"RuntimeError": ["trial-2"]},
                }
            },
        },
    }
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
PY
SH
  chmod +x "$path"
}

assert_extra_compose_arg() {
  local capture_file="$1"
  local overlay_file="$2"
  python3 - "$capture_file" "$overlay_file" <<'PY'
import sys
from pathlib import Path

capture = Path(sys.argv[1])
overlay = sys.argv[2]
args = [part.decode() for part in capture.read_bytes().split(b"\0") if part]
try:
    index = args.index("--extra-docker-compose")
except ValueError:
    raise SystemExit(f"missing --extra-docker-compose in command: {args!r}")
if index == len(args) - 1:
    raise SystemExit(f"--extra-docker-compose is missing its path value: {args!r}")
if args[index + 1] != overlay:
    raise SystemExit(
        f"unexpected extra compose path {args[index + 1]!r}; expected {overlay!r}"
    )
PY
}

assert_structured_mount_arg() {
  local capture_file="$1"
  local source_dir="$2"
  local target_dir="$3"
  python3 - "$capture_file" "$source_dir" "$target_dir" <<'PY'
import json
import sys
from pathlib import Path

capture = Path(sys.argv[1])
source = sys.argv[2]
target = sys.argv[3]
args = [part.decode() for part in capture.read_bytes().split(b"\0") if part]
try:
    index = args.index("--mounts-json")
except ValueError:
    raise SystemExit(f"missing --mounts-json in command: {args!r}")
if index == len(args) - 1:
    raise SystemExit(f"--mounts-json is missing its JSON value: {args!r}")
mounts = json.loads(args[index + 1])
if not all(isinstance(mount, dict) for mount in mounts):
    raise SystemExit(f"mounts must be structured objects: {mounts!r}")
expected = {
    "type": "bind",
    "source": source,
    "target": target,
    "read_only": True,
}
if expected not in mounts:
    raise SystemExit(f"missing expected mount {expected!r}: {mounts!r}")
PY
}

assert_arg_pair() {
  local capture_file="$1"
  local option="$2"
  local expected="$3"
  python3 - "$capture_file" "$option" "$expected" <<'PY'
import sys
from pathlib import Path

args = [part.decode() for part in Path(sys.argv[1]).read_bytes().split(b"\0") if part]
option, expected = sys.argv[2:]
if not any(args[index:index + 2] == [option, expected] for index in range(len(args) - 1)):
    raise SystemExit(f"missing {option} {expected!r} in command: {args!r}")
PY
}

assert_file_content() {
  local path="$1"
  local expected="$2"
  local actual
  if [[ ! -f "$path" ]]; then
    echo "expected file is missing: $path" >&2
    return 1
  fi
  actual="$(cat "$path")"
  if [[ "$actual" != "$expected" ]]; then
    echo "unexpected content in $path: '$actual' (expected '$expected')" >&2
    return 1
  fi
}

assert_arg_absent() {
  local capture_file="$1"
  local needle="$2"
  python3 - "$capture_file" "$needle" <<'PY'
import sys
from pathlib import Path

args = [part.decode() for part in Path(sys.argv[1]).read_bytes().split(b"\0") if part]
needle = sys.argv[2]
if any(needle in arg for arg in args):
    raise SystemExit(f"unexpected {needle!r} in command: {args!r}")
PY
}

assert_exact_arg_absent() {
  local capture_file="$1"
  local unexpected="$2"
  python3 - "$capture_file" "$unexpected" <<'PY'
import sys
from pathlib import Path

args = [part.decode() for part in Path(sys.argv[1]).read_bytes().split(b"\0") if part]
unexpected = sys.argv[2]
if unexpected in args:
    raise SystemExit(f"unexpected exact argument {unexpected!r} in command: {args!r}")
PY
}

assert_mount_source_absent() {
  local capture_file="$1"
  local needle="$2"
  python3 - "$capture_file" "$needle" <<'PY'
import json
import sys
from pathlib import Path

args = [part.decode() for part in Path(sys.argv[1]).read_bytes().split(b"\0") if part]
needle = sys.argv[2]
if "--mounts-json" in args:
    mounts = json.loads(args[args.index("--mounts-json") + 1])
    for mount in mounts:
        if needle in mount.get("source", ""):
            raise SystemExit(f"unexpected mount source with {needle!r}: {mounts!r}")
PY
}

run_harboropik() {
  local agent="$1"
  local capture_bin="$2"
  local capture_file="$3"
  local output_dir="$4"
  local dataset_name="${5:-example/dataset@1.0}"
  local include_tasks="${6:-}"
  local trace="${7:-true}"
  local queue_worker="${8:-1}"
  local min_test="${9:-0}"
  local runs="${10:-1}"
  local n_concurrent="${11:-1}"
  local judge_base_url="${12:-}"
  local judge_api_key="${13:-}"
  local judge_model="${14:-}"
  local opik_base="http://opik.example"
  local opik_url_override="http://opik.example/api"
  local hook_flag="1"
  if [[ "$trace" == "false" ]]; then
    # No Opik configuration at all: the run must still work, and the
    # hook default must follow the disabled tracing switch.
    opik_base=""
    opik_url_override=""
    hook_flag=""
  fi
  local fake_bin
  local trace_dir
  local wheel_dir
  local dataset_path
  fake_bin="$(dirname "$capture_bin")"
  mkdir -p "$output_dir"
  mkdir -p "$output_dir/run/runtime/$agent"
  mkdir -p "$output_dir/run/queue/$agent"
  dataset_path="$output_dir/dataset"
  mkdir -p "$dataset_path"
  wheel_dir="$output_dir/wheels"
  mkdir -p "$wheel_dir"
  trace_dir="$output_dir/trace"
  if [[ "$trace" != "false" ]]; then
    mkdir -p \
      "$trace_dir/src/sii_opik_plugin/claude_code" \
      "$trace_dir/src/sii_opik_plugin/opencode" \
      "$trace_dir/harness/opencode"
    : >"$trace_dir/src/sii_opik_plugin/claude_code/claude_realtime_trace.py"
    : >"$trace_dir/src/sii_opik_plugin/opencode/opencode_realtime_trace.py"
    : >"$trace_dir/harness/opencode/opik-trace.ts"
  fi

  local log_file="$output_dir/$agent.log"
  if ! env -i \
    PATH="$fake_bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$output_dir/home" \
    AGENT="$agent" \
    DATASET_NAME="$dataset_name" \
    DATASET_PATH="$dataset_path" \
    INCLUDE_TASKS="$include_tasks" \
    OUTPUT_PATH="$output_dir/run" \
    HARBOR_QUEUE_WORKER="$queue_worker" \
    OPIK_URL="$opik_url_override" \
    OPIK_BASE="$opik_base" \
    OPIK_URL_OVERRIDE="$opik_url_override" \
    OPIK_API_KEY="fake-opik-key" \
    BASE_URL="http://llm.example" \
    API_KEY="fake-llm-key" \
    JUDGE_BASE_URL="$judge_base_url" \
    JUDGE_API_KEY="$judge_api_key" \
    JUDGE_MODEL="$judge_model" \
    MODEL="fake-model" \
    MIN_TEST="$min_test" \
    MIN_TEST_INCLUDE_TASK="fix-git" \
    HARBOR_CC_OPIK_ENABLE_HOOK="$hook_flag" \
    HARBOR_CC_PY_WHEEL_DIR_SOURCE="$wheel_dir" \
    TRACE_PLUGIN_SOURCE_DIR="$trace_dir" \
    HARBOR_SKIP_DOCKERHUB_PREFLIGHT="1" \
    HARBOR_RUNS="$runs" \
    N_ATTEMPTS="1" \
    HARBOR_N_CONCURRENT="$n_concurrent" \
    TOTAL_WORKERS="1" \
    HARBOR_MAX_RETRIES="0" \
    HARBOR_CAPTURE_FILE="$capture_file" \
    HARBOR_CAPTURE_RESULT="1" \
    HARBOR_OPIK_BIN="$capture_bin" \
    HARBOR_CLI_BIN="$capture_bin" \
    HARBOR_OPIK_PYTHON="$capture_bin" \
    HARBOR_RUNNER_PREPARE="0" \
    OPENCODE_CONFIG_CONTENT="{}" \
    bash "$HARBOR_DIR/harboropik.sh" >"$log_file" 2>&1; then
    cat "$log_file" >&2
    return 1
  fi
}

assert_registry_summary() {
  local summary="$1"
  local pattern
  for pattern in \
    '^status: +complete$' \
    '^DATASET_NAME: codepde@1\.0$' \
    '^MODEL: +fake-model$' \
    '^harbor_exit_code: 0$' \
    '^total: +2$' \
    '^completed: +2$' \
    '^errored: +1$' \
    '^mean_reward: +0\.5$' \
    '^  reward=1\.0: 1$' \
    '^Harbor stats:$' \
    '^ +"1\.0": \[$' \
    '^ +"RuntimeError": \[$' \
    '^  result: +.*/fake-run/result\.json$'
  do
    if ! grep -Eq "$pattern" "$summary"; then
      cat "$summary" >&2
      echo "registry summary missing expected pattern: $pattern" >&2
      return 1
    fi
  done
}

assert_registry_summary_requires_result() {
  local output="$1"
  local summary="$output/summary.txt"
  mkdir -p "$output"

  RUN_ID="missing-result" AGENT="claude-code" \
    python3 "$HARBOR_DIR/scripts/write_harbor_registry_summary.py" \
      "$output/missing-job" "$summary" 0 "codepde@1.0"

  grep -Eq '^status: +failed$' "$summary"
  grep -q '^failure_reason: Harbor exited without an aggregate result$' "$summary"
  grep -q '^Harbor result summary: unavailable$' "$summary"
}

main() {
  local tmp fake_bin default_overlay claude_capture opencode_capture pi_capture capture_bin
  local seta_capture sweverify_capture registry_capture traceoff_capture traceoff_oc_capture
  local opencode_registry_capture opencode_local_capture
  local min_test_capture
  local deepsearchqa_capture deepsearchqa_opencode_capture deepsearchqa_oracle_capture
  tmp="$(mktemp -d)"
  TEST_TMP_DIR="$tmp"
  trap 'rm -rf "$TEST_TMP_DIR"' EXIT

  fake_bin="$tmp/bin"
  make_fake_bin "$fake_bin"
  capture_bin="$fake_bin/capture"
  make_capture_bin "$capture_bin"

  default_overlay="$HARBOR_DIR/overlays/unprivileged-task.yaml"

  claude_capture="$tmp/claude-default.args"
  run_harboropik \
    "claude-code" "$capture_bin" "$claude_capture" "$tmp/claude-default" \
    "codepde@1.0"
  assert_extra_compose_arg "$claude_capture" "$default_overlay"
  assert_arg_pair "$claude_capture" "--dataset" "codepde@1.0"
  assert_arg_pair "$claude_capture" "--ae" "HARBOR_DATASET=codepde@1.0"
  assert_file_content \
    "${claude_capture}.verifier-tools" \
    "curl,env,uv,uvx"
  assert_arg_pair \
    "$claude_capture" \
    "--ve" \
    "HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin"

  registry_capture="$tmp/claude-registry.args"
  run_harboropik \
    "claude-code" "$capture_bin" "$registry_capture" "$tmp/claude-registry" \
    "codepde@1.0" "" "true" "0"
  assert_file_content "${registry_capture}.pid-identity" "valid"
  assert_registry_summary "$tmp/claude-registry/run/summary.txt"
  assert_registry_summary_requires_result "$tmp/registry-missing-result"

  deepsearchqa_capture="$tmp/deepsearchqa.args"
  run_harboropik \
    "claude-code" "$capture_bin" "$deepsearchqa_capture" "$tmp/deepsearchqa" \
    "deepsearchqa" "" "true" "1" "0" "1" "1" \
    "https://judge.example/v1/chat/completions" "fake-judge-key" "judge-model"
  assert_arg_pair "$deepsearchqa_capture" "--dataset" "kgmon/deepsearchqa"
  assert_arg_pair \
    "$deepsearchqa_capture" \
    "--verifier" \
    "deepsearchqa_verifier:DeepSearchQAVerifier"
  assert_arg_absent "$deepsearchqa_capture" "fake-judge-key"
  assert_arg_pair \
    "$deepsearchqa_capture" \
    "--ak" \
    "disallowed_tools=RemoteTrigger AskUserQuestion"

  deepsearchqa_opencode_capture="$tmp/deepsearchqa-opencode.args"
  run_harboropik \
    "opencode" "$capture_bin" "$deepsearchqa_opencode_capture" \
    "$tmp/deepsearchqa-opencode" "deepsearchqa" "" "true" "1" "0" "1" "1" \
    "https://judge.example/v1/chat/completions" "fake-judge-key" "judge-model"
  assert_arg_pair \
    "$deepsearchqa_opencode_capture" \
    "--verifier" \
    "deepsearchqa_verifier:DeepSearchQAVerifier"
  assert_arg_absent "$deepsearchqa_opencode_capture" "fake-judge-key"

  deepsearchqa_oracle_capture="$tmp/deepsearchqa-oracle.args"
  run_harboropik \
    "oracle" "$capture_bin" "$deepsearchqa_oracle_capture" \
    "$tmp/deepsearchqa-oracle" "deepsearchqa" "" "true" "1" "0" "1" "1" \
    "https://judge.example/v1/chat/completions" "fake-judge-key" "judge-model"
  assert_arg_pair \
    "$deepsearchqa_oracle_capture" \
    "--verifier" \
    "deepsearchqa_verifier:DeepSearchQAVerifier"
  assert_arg_absent "$deepsearchqa_oracle_capture" "fake-judge-key"

  opencode_capture="$tmp/opencode-default.args"
  run_harboropik \
    "opencode" "$capture_bin" "$opencode_capture" "$tmp/opencode-default" \
    "terminalbench21" "fix-git"
  assert_extra_compose_arg "$opencode_capture" "$default_overlay"
  assert_arg_pair "$opencode_capture" "--dataset" "terminal-bench/terminal-bench-2-1"
  assert_arg_pair \
    "$opencode_capture" \
    "--ae" \
    "HARBOR_DATASET=terminal-bench/terminal-bench-2-1"
  assert_arg_pair "$opencode_capture" "-i" "terminal-bench/fix-git"
  assert_arg_absent "$opencode_capture" "fake-llm-key"
  assert_arg_absent "$opencode_capture" "OPENCODE_RUNTIME_SECRETS_JSON"
  assert_structured_mount_arg \
    "$opencode_capture" \
    "$tmp/opencode-default/wheels" \
    "/opt/tb-opik/python-wheels"
  assert_file_content \
    "${opencode_capture}.verifier-tools" \
    "curl,env,uv,uvx"
  assert_arg_pair \
    "$opencode_capture" \
    "--ve" \
    "HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin"

  pi_capture="$tmp/pi-default.args"
  run_harboropik \
    "pi" "$capture_bin" "$pi_capture" "$tmp/pi-default" \
    "terminalbench21" "fix-git"
  assert_extra_compose_arg "$pi_capture" "$default_overlay"
  assert_arg_pair "$pi_capture" "--dataset" "terminal-bench/terminal-bench-2-1"
  assert_arg_pair "$pi_capture" "-i" "terminal-bench/fix-git"
  assert_arg_pair "$pi_capture" "--agent-import-path" "pi_harbor:AgentFleetPi"
  assert_exact_arg_absent "$pi_capture" "-a"
  assert_arg_pair "$pi_capture" "-m" "llm.example/fake-model"
  assert_arg_pair "$pi_capture" "--ak" "version=0.81.1"
  assert_exact_arg_absent "$pi_capture" "thinking=high"
  assert_arg_pair "$pi_capture" "--ae" "AGENT_FLEET_API_KEY=fake-llm-key"
  assert_arg_pair "$pi_capture" "--ae" "PI_OFFLINE=1"
  assert_arg_pair \
    "$pi_capture" \
    "--ae" \
    "PI_NODE_RUNTIME_PATH=/opt/tb-opik/python-wheels/pi-node-runtime.tar.gz"
  assert_arg_pair \
    "$pi_capture" \
    "--ae" \
    "PI_RUNTIME_TAR_PATH=/opt/tb-opik/python-wheels/pi-runtime-0.81.1.tar.gz"
  assert_arg_absent "$pi_capture" "PI_TGZ_PATH="
  assert_arg_pair \
    "$pi_capture" \
    "--ae" \
    "NO_PROXY=127.0.0.1,localhost,host.docker.internal,opik.example,llm.example"
  assert_arg_absent "$pi_capture" "disallowed_tools="
  assert_arg_absent "$pi_capture" "max_turns="
  assert_structured_mount_arg \
    "$pi_capture" \
    "$tmp/pi-default/wheels" \
    "/opt/tb-opik/python-wheels"

  opencode_registry_capture="$tmp/opencode-registry.args"
  run_harboropik \
    "opencode" "$capture_bin" "$opencode_registry_capture" "$tmp/opencode-registry" \
    "terminalbench21" "" "true" "0" "0" "1" "20"
  assert_arg_pair "$opencode_registry_capture" "--n-concurrent" "20"

  opencode_local_capture="$tmp/opencode-local.args"
  run_harboropik \
    "opencode" "$capture_bin" "$opencode_local_capture" "$tmp/opencode-local" \
    "auto" "" "true" "0" "0" "1" "20"
  assert_arg_pair "$opencode_local_capture" "--n-concurrent" "1"

  seta_capture="$tmp/seta-default.args"
  run_harboropik \
    "opencode" "$capture_bin" "$seta_capture" "$tmp/seta-default" \
    "seta" "0"
  assert_arg_pair "$seta_capture" "--dataset" "seta-env"
  assert_arg_pair "$seta_capture" "-i" "0"

  sweverify_capture="$tmp/sweverify-default.args"
  run_harboropik \
    "opencode" "$capture_bin" "$sweverify_capture" "$tmp/sweverify-default" \
    "sweverify" "astropy__astropy-12907"
  assert_arg_pair "$sweverify_capture" "--dataset" "swebench-verified"
  assert_arg_pair "$sweverify_capture" "-i" "astropy__astropy-12907"

  min_test_capture="$tmp/claude-min-test.args"
  run_harboropik \
    "claude-code" "$capture_bin" "$min_test_capture" "$tmp/claude-min-test" \
    "terminalbench21" "" "true" "1" "1" "10"
  assert_arg_pair "$min_test_capture" "-k" "1"
  assert_arg_pair "$min_test_capture" "-l" "1"
  assert_arg_pair "$min_test_capture" "-i" "terminal-bench/fix-git"
  grep -q 'MIN_TEST=1 enabled' "$tmp/claude-min-test/claude-code.log"

  # OPIK_URL empty with no Opik configuration at all: the run must
  # still construct the benchmark command, with the realtime hook off.
  traceoff_capture="$tmp/claude-traceoff.args"
  run_harboropik \
    "claude-code" "$capture_bin" "$traceoff_capture" "$tmp/claude-traceoff" \
    "codepde@1.0" "" "false"
  assert_arg_pair "$traceoff_capture" "--dataset" "codepde@1.0"
  assert_arg_pair "$traceoff_capture" "--ae" "CC_OPIK_ENABLE_HOOK=false"
  assert_file_content "${traceoff_capture}.opik-track-disable" "true"
  assert_file_content "${traceoff_capture}.opik-environment" "{}"
  # Trace-off keeps the agent runtime cache mounted while dropping the hook
  # mount and every Opik connection field from the task environment.
  assert_structured_mount_arg \
    "$traceoff_capture" \
    "$tmp/claude-traceoff/wheels" \
    "/opt/tb-opik/python-wheels"
  assert_mount_source_absent "$traceoff_capture" "claude_realtime_trace"
  assert_arg_absent "$traceoff_capture" "OPIK_API_KEY="
  assert_arg_absent "$traceoff_capture" "OPIK_URL="

  traceoff_oc_capture="$tmp/opencode-traceoff.args"
  run_harboropik \
    "opencode" "$capture_bin" "$traceoff_oc_capture" "$tmp/opencode-traceoff" \
    "terminalbench21" "fix-git" "false"
  assert_arg_pair "$traceoff_oc_capture" "--dataset" "terminal-bench/terminal-bench-2-1"
  assert_arg_pair "$traceoff_oc_capture" "-i" "terminal-bench/fix-git"
  assert_file_content "${traceoff_oc_capture}.opik-track-disable" "true"
  assert_file_content "${traceoff_oc_capture}.opik-environment" "{}"
  assert_arg_absent "$traceoff_oc_capture" "OPIK_API_KEY="
  assert_arg_absent "$traceoff_oc_capture" "OPIK_URL="

  # The tracing control case still forwards the connection fields.
  assert_arg_pair "$claude_capture" "--ae" "OPIK_API_KEY=fake-opik-key"
  assert_arg_pair "$opencode_capture" "--ae" "OPIK_API_KEY=fake-opik-key"
}

main "$@"
