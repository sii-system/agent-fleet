#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PROJECT_DIR="$TMP_DIR/repo"
mkdir -p \
  "$PROJECT_DIR/scripts/dind" \
  "$PROJECT_DIR/Agents/utils/common/Harbor" \
  "$TMP_DIR/bin"
cp "$REPO_ROOT/scripts/dind-run.sh" "$PROJECT_DIR/scripts/dind-run.sh"
if grep -q -- 'docker_exec_root_env' "$PROJECT_DIR/scripts/dind-run.sh"; then
  echo "dind-run.sh still defines or uses the root setup helper" >&2
  exit 1
fi
# First-occurrence rewrite via python: GNU sed's -i and 0,/re/ addressing
# are unavailable in the BSD sed shipped on macOS.
python3 - "$PROJECT_DIR/scripts/dind-run.sh" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = "if running_in_container; then"
new = 'if [[ "${DIND_TEST_ASSUME_HOST:-0}" != "1" ]] && running_in_container; then'
if old not in text:
    sys.exit("test guard patch point not found in dind-run.sh")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
PY
cp "$REPO_ROOT/scripts/dind/dockerd-entrypoint.sh" "$PROJECT_DIR/scripts/dind/dockerd-entrypoint.sh"
cp "$REPO_ROOT/scripts/dind/prepare-cgroup-v2.sh" "$PROJECT_DIR/scripts/dind/prepare-cgroup-v2.sh"
cp "$REPO_ROOT/scripts/run_fleet.sh" "$PROJECT_DIR/scripts/run_fleet.sh"
cp "$REPO_ROOT/scripts/config_loader.sh" "$PROJECT_DIR/scripts/config_loader.sh"
cp "$REPO_ROOT/scripts/prerequisites.sh" "$PROJECT_DIR/scripts/prerequisites.sh"
cp "$REPO_ROOT/scripts/fleet_spec_io.sh" "$PROJECT_DIR/scripts/fleet_spec_io.sh"
cp "$REPO_ROOT/scripts/fleet_spec_validate.jq" "$PROJECT_DIR/scripts/fleet_spec_validate.jq"
cp \
  "$REPO_ROOT/Agents/utils/common/Harbor/runner-requirements.txt" \
  "$PROJECT_DIR/Agents/utils/common/Harbor/runner-requirements.txt"
if grep -q -- 'tcp://0.0.0.0:2375' "$PROJECT_DIR/scripts/dind/dockerd-entrypoint.sh"; then
  echo "DinD entrypoint exposes an unauthenticated TCP daemon" >&2
  exit 1
fi
chmod +x "$PROJECT_DIR/scripts/dind-run.sh"
touch "$PROJECT_DIR/scripts/setup.sh"
touch "$PROJECT_DIR/scripts/dind/Dockerfile"
chmod +x "$PROJECT_DIR/scripts/setup.sh" "$PROJECT_DIR/scripts/run_fleet.sh"
export DIND_TEST_ASSUME_HOST=1
unset BASE_URL API_KEY MODEL
unset ANTHROPIC_BASE_URL AUTH_TOKEN ANTHROPIC_AUTH_TOKEN TB_MODEL
unset HARBOR_TEMPERATURE HARBOR_TOP_P HARBOR_MAX_TOKENS
unset TRACE_TO_OPIK OPIK_URL OPIK_API_KEY OPIK_WORKSPACE OPIK_PROJECT_NAME
unset HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST NPM_CONFIG_REGISTRY
unset DIND_REGISTRY_MIRRORS DIND_REGISTRY_MIRROR DIND_DEFAULT_ADDRESS_POOLS

cat > "$PROJECT_DIR/config.env" <<'EOF'
BASE_URL=https://config.example.com
API_KEY=sk-config
MODEL=config-model
DIND_REGISTRY_MIRRORS=https://config-mirror.invalid
DIND_DEFAULT_ADDRESS_POOLS=base=10.100.0.0/16,size=21
EOF

cat > "$PROJECT_DIR/config.local.env" <<'EOF'
BASE_URL=https://local.example.com
API_KEY=sk-local
MODEL=local-model
OPIK_API_KEY=opik-local
PIP_INDEX_URL=https://packages.example.com/simple
NPM_CONFIG_REGISTRY=https://npm.example.com
DIND_REGISTRY_MIRRORS="https://docker.m.daocloud.io, https://mirror.ccs.tencentyun.com"
DIND_DEFAULT_ADDRESS_POOLS="base=10.200.0.0/13,size=21;base=172.16.0.0/12,size=20"
EOF

LOG="$TMP_DIR/docker.log"
DOCKER_ACTION_LOG="$TMP_DIR/docker-actions.log"
DOCKER_ENV_CAPTURE_LOG="$TMP_DIR/docker-env-capture.log"
DOCKER_SIGNAL_LOG="$TMP_DIR/docker-signal.log"
DOCKER_SIGNAL_HELPER_LOG="$TMP_DIR/docker-signal-helper.log"
DOCKER_SIGNAL_TARGET_PID_FILE="$TMP_DIR/docker-signal-target.pid"
DOCKER_ACTIVE_ENV_FILE_PATH_FILE="$TMP_DIR/docker-active-env-file-path"
export \
  DOCKER_ACTION_LOG \
  DOCKER_ENV_CAPTURE_LOG \
  DOCKER_SIGNAL_LOG \
  DOCKER_SIGNAL_HELPER_LOG \
  DOCKER_SIGNAL_TARGET_PID_FILE \
  DOCKER_ACTIVE_ENV_FILE_PATH_FILE
cat > "$TMP_DIR/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "$DOCKER_ACTION_LOG"
printf 'docker'
for arg in "$@"; do
  printf ' <%s>' "$arg"
done
printf '\n'

if [[ "${1:-}" == "exec" ]]; then
  env_file=""
  previous=""
  for arg in "$@"; do
    if [[ "$previous" == "--env-file" ]]; then
      env_file="$arg"
      break
    fi
    previous="$arg"
  done
  if [[ -n "$env_file" ]]; then
    if [[ ! -f "$env_file" ]]; then
      echo "docker exec env file does not exist: $env_file" >&2
      exit 1
    fi
    if mode="$(stat -c '%a' "$env_file" 2>/dev/null)"; then
      :
    else
      mode="$(stat -f '%Lp' "$env_file")"
    fi
    {
      printf 'BEGIN %s\n' "$*"
      printf 'ENV_FILE %s\n' "$env_file"
      printf 'MODE %s\n' "$mode"
      while IFS= read -r entry || [[ -n "$entry" ]]; do
        printf 'ENV %s\n' "$entry"
      done < "$env_file"
      printf 'END\n'
    } >> "$DOCKER_ENV_CAPTURE_LOG"
    printf '%s\n' "$env_file" > "$DOCKER_ACTIVE_ENV_FILE_PATH_FILE"
  fi
fi

if [[ "${1:-}" == "exec" &&
      "$*" == *"agent-fleet-dind-exec-signal"* ]]; then
  signal="${!#}"
  active_env_file="$(cat "$DOCKER_ACTIVE_ENV_FILE_PATH_FILE")"
  state="present"
  [[ ! -e "$active_env_file" ]] && state="removed"
  printf '%s env_file=%s\n' \
    "$signal" "$state" > "$DOCKER_SIGNAL_HELPER_LOG"
  target_pid="$(cat "$DOCKER_SIGNAL_TARGET_PID_FILE")"
  kill "-$signal" "$target_pid"
  exit 0
fi

if [[ "${1:-}" == "ps" ]]; then
  exit 0
fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  exit 1
fi
if [[ "${1:-}" == "exec" && "$*" == *"docker info"* ]]; then
  exit 0
fi
if [[ "${1:-}" == "exec" && "$*" == *"command -v pi"* ]]; then
  case "${MOCK_CONTAINER_PATHS_SOURCE:-}" in
    AGENT_FLEET_PATHS_FILE)
      [[ "$*" == *'${AGENT_FLEET_PATHS_FILE:-'* ]] || exit 1
      ;;
    XDG_CONFIG_HOME)
      [[ "$*" == *'${XDG_CONFIG_HOME:-$4/.config}'* ]] || exit 1
      ;;
  esac
  if [[ "${MOCK_PATHS_ENV_MISSING:-0}" == "1" &&
        "$*" == *"agent-fleet/paths.env"* ]]; then
    exit 1
  fi
fi
if [[ "${1:-}" == "exec" && "${MOCK_FAIL_RUN_FLEET:-0}" == "1" &&
      "$*" == *"./scripts/run_fleet.sh"* ]]; then
  exit 42
fi
if [[ "${1:-}" == "exec" && -n "${MOCK_SIGNAL_RUN_FLEET:-}" &&
      "$*" == *"./scripts/run_fleet.sh"* ]]; then
  signal="$MOCK_SIGNAL_RUN_FLEET"
  case "$signal" in
    INT|TERM)
      ;;
    *)
      echo "unsupported mock signal: $signal" >&2
      exit 1
      ;;
  esac
  printf '%s\n' "$$" > "$DOCKER_SIGNAL_TARGET_PID_FILE"
  record_signal_state() {
    local state="present"
    [[ ! -e "$env_file" ]] && state="removed"
    printf '%s env_file=%s\n' "$signal" "$state" > "$DOCKER_SIGNAL_LOG"
    exit 0
  }
  trap record_signal_state "$signal"
  kill "-$signal" "$PPID"
  sleep 1
  state="present"
  [[ ! -e "$env_file" ]] && state="removed"
  printf 'natural-after-%s env_file=%s\n' \
    "$signal" "$state" > "$DOCKER_SIGNAL_LOG"
  exit 0
fi
exit 0
MOCK
chmod +x "$TMP_DIR/bin/docker"

assert_env_files_removed() {
  local context="$1" env_file leaked=0
  while IFS= read -r env_file; do
    if [[ "$env_file" == "$PROJECT_DIR" || "$env_file" == "$PROJECT_DIR/"* ]]; then
      echo "$context used an env file inside the repository: $env_file" >&2
      leaked=1
    fi
    if [[ -e "$env_file" ]]; then
      echo "$context left its env file behind: $env_file" >&2
      rm -f -- "$env_file"
      leaked=1
    fi
  done < <(sed -n 's/^ENV_FILE //p' "$DOCKER_ENV_CAPTURE_LOG")
  [[ "$leaked" == "0" ]]
}

: > "$DOCKER_ENV_CAPTURE_LOG"
PATH="$TMP_DIR/bin:$PATH" \
DIND_BOOTSTRAP=always \
DIND_USER_UID=1234 \
DIND_USER_GID=5678 \
HTTP_PROXY=http://proxy.invalid:8080 \
HTTPS_PROXY=http://proxy.invalid:8443 \
NO_PROXY=existing.example \
TRACE_TO_OPIK=false \
MIN_TEST=1 \
MIN_TEST_INCLUDE_TASK=custom-canary \
HARBOR_TEMPERATURE=0.2 \
HARBOR_TOP_P= \
HARBOR_MAX_TOKENS=8192 \
"$PROJECT_DIR/scripts/dind-run.sh" --taskset terminalbench21 --agent claude-code --workers 1 > "$LOG"

grep -q -- '--registry-mirror=https://docker.m.daocloud.io' "$LOG"
grep -q -- '--registry-mirror=https://mirror.ccs.tencentyun.com' "$LOG"
grep -q -- '--default-address-pool=base=10.200.0.0/13,size=21' "$LOG"
grep -q -- '--default-address-pool=base=172.16.0.0/12,size=20' "$LOG"
grep -q -- '<--label> <agent-fleet.default-address-pools=base=10.200.0.0/13,size=21;base=172.16.0.0/12,size=20>' "$LOG"
grep -q -- '<-v> <agent-fleet-dind-docker:/var/lib/docker>' "$LOG"
grep -q -- '<-v> <agent-fleet-dind-home:/home/agent>' "$LOG"
grep -q -- "<-v> <$PROJECT_DIR:$PROJECT_DIR>" "$LOG"
grep -q -- '<-e> <HTTP_PROXY=http://proxy.invalid:8080>' "$LOG"
grep -q -- '<-e> <HTTPS_PROXY=http://proxy.invalid:8443>' "$LOG"
grep -q -- '<-e> <NO_PROXY=existing.example,127.0.0.1,localhost,host.docker.internal,local.example.com>' "$LOG"
grep -q -- '<-e> <no_proxy=existing.example,127.0.0.1,localhost,host.docker.internal,local.example.com>' "$LOG"
RUNNER_IMAGE="$(grep -Eo 'agent-fleet-dind:28-[0-9a-f]{12}' "$LOG" | head -n 1 || true)"
if [[ -z "$RUNNER_IMAGE" ]]; then
  echo "default runner image tag is not fingerprinted" >&2
  exit 1
fi
grep -q -- '<--build-arg> <DIND_BASE_IMAGE=m.daocloud.io/docker.io/library/debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818>' "$LOG"
grep -q -- '<--build-arg> <UV_IMAGE=m.daocloud.io/ghcr.io/astral-sh/uv:0.11.28>' "$LOG"
grep -q -- "<-f> <$PROJECT_DIR/scripts/dind/Dockerfile> <-t> <$RUNNER_IMAGE> <$PROJECT_DIR>" "$LOG"
grep -q -- "<--label> <agent-fleet.runner-image=$RUNNER_IMAGE>" "$LOG"
grep -q -- "^docker <run> .* <-w> <$PROJECT_DIR> <$RUNNER_IMAGE> " "$LOG"
for secret in sk-local opik-local; do
  if grep -Fq -- "$secret" "$LOG" || grep -Fq -- "$secret" "$DOCKER_ACTION_LOG"; then
    echo "dind-run.sh exposed a fake credential in Docker argv: $secret" >&2
    exit 1
  fi
done
grep -Eq -- '^docker <exec> <--user> <agent> <--env-file> </tmp/agent-fleet-dind-env\.[^>]+> <agent-fleet-dind> <sh> <-c> <pid_file=\$1;.*> <agent-fleet-dind-exec> </home/agent/\.agent-fleet-dind-exec\.[^>]+\.pid> <./scripts/setup.sh>$' "$LOG"
grep -Eq -- '^docker <exec> <--user> <agent> <--env-file> </tmp/agent-fleet-dind-env\.[^>]+> <agent-fleet-dind> <sh> <-c> <pid_file=\$1;.*> <agent-fleet-dind-exec> </home/agent/\.agent-fleet-dind-exec\.[^>]+\.pid> <./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>$' "$LOG"
if grep -Eq -- '^docker <exec> <--user> <agent> .* <env> .*<./scripts/(setup|run_fleet).sh>' "$LOG"; then
  echo "dind-run.sh still passes execution variables through an in-container env argv" >&2
  exit 1
fi
env_capture_count="$(grep -c '^BEGIN ' "$DOCKER_ENV_CAPTURE_LOG" || true)"
secure_mode_count="$(grep -c '^MODE 600$' "$DOCKER_ENV_CAPTURE_LOG" || true)"
if [[ "$env_capture_count" != "2" || "$secure_mode_count" != "2" ]]; then
  echo "setup and benchmark must each receive the same mode-0600 env file" >&2
  exit 1
fi
for expected_env in \
  "REPO_DIR=$PROJECT_DIR" \
  "BASE_URL=https://local.example.com" \
  "API_KEY=sk-local" \
  "MODEL=local-model" \
  "HOME=/home/agent" \
  "HTTP_PROXY=http://proxy.invalid:8080" \
  "HTTPS_PROXY=http://proxy.invalid:8443" \
  "TRACE_TO_OPIK=false" \
  "MIN_TEST=1" \
  "MIN_TEST_INCLUDE_TASK=custom-canary" \
  "HARBOR_TEMPERATURE=0.2" \
  "HARBOR_TOP_P=" \
  "HARBOR_MAX_TOKENS=8192" \
  "OPIK_API_KEY=opik-local" \
  "PIP_INDEX_URL=https://packages.example.com/simple" \
  "NPM_CONFIG_REGISTRY=https://npm.example.com"; do
  if [[ "$(grep -Fxc -- "ENV $expected_env" "$DOCKER_ENV_CAPTURE_LOG" || true)" != "2" ]]; then
    echo "setup and benchmark did not both receive: ${expected_env%%=*}" >&2
    exit 1
  fi
done
assert_env_files_removed "successful DinD run"
if grep -q -- '<sh> <-lc>.*apk add' "$LOG"; then
  echo "dind-run.sh installed dependencies inside the running DinD container" >&2
  exit 1
fi
grep -q -- '<./scripts/setup.sh>' "$LOG"
if grep -q -- '^docker <exec> <agent-fleet-dind> .* <./scripts/setup.sh>$' "$LOG"; then
  echo "dind-run.sh ran setup as container root" >&2
  exit 1
fi
home_chown_count="$(
  grep -c -- '^docker <exec> <agent-fleet-dind> <chown> <-R> <1234:5678> </home/agent>$' "$LOG" ||
    true
)"
if [[ "$home_chown_count" != "1" ]]; then
  echo "dind-run.sh should prepare the home volume once before non-root setup" >&2
  exit 1
fi
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$LOG"

: > "$DOCKER_ENV_CAPTURE_LOG"
ALIAS_LOG="$TMP_DIR/runtime-aliases.log"
PATH="$TMP_DIR/bin:$PATH" \
ANTHROPIC_BASE_URL=https://runtime-alias.example.com \
AUTH_TOKEN=fake-runtime-alias-key \
TB_MODEL=runtime-alias-model \
TRACE_TO_OPIK=false \
DIND_BOOTSTRAP=always \
"$PROJECT_DIR/scripts/dind-run.sh" \
  --taskset terminalbench21 --agent claude-code --workers 1 > "$ALIAS_LOG"

for expected_env in \
  "BASE_URL=https://local.example.com" \
  "API_KEY=sk-local" \
  "MODEL=local-model"; do
  if [[ "$(grep -Fxc -- "ENV $expected_env" "$DOCKER_ENV_CAPTURE_LOG" || true)" != "2" ]]; then
    echo "tool alias polluted DinD global config: ${expected_env%%=*}" >&2
    exit 1
  fi
done
if grep -Fq -- 'fake-runtime-alias-key' "$ALIAS_LOG" ||
   grep -Fq -- 'fake-runtime-alias-key' "$DOCKER_ACTION_LOG"; then
  echo "runtime alias credential was exposed in Docker argv" >&2
  exit 1
fi
assert_env_files_removed "runtime alias DinD run"

: > "$DOCKER_ENV_CAPTURE_LOG"
AUTH_ONLY_LOG="$TMP_DIR/runtime-auth-token.log"
PATH="$TMP_DIR/bin:$PATH" \
API_KEY= \
AUTH_TOKEN=fake-runtime-auth-only \
TRACE_TO_OPIK=false \
DIND_BOOTSTRAP=always \
"$PROJECT_DIR/scripts/dind-run.sh" \
  --taskset terminalbench21 --agent claude-code --workers 1 > "$AUTH_ONLY_LOG"

if [[ "$(grep -Fxc -- "ENV API_KEY=fake-runtime-auth-only" "$DOCKER_ENV_CAPTURE_LOG" || true)" != "2" ]]; then
  echo "AUTH_TOKEN did not supply the missing DinD API_KEY" >&2
  exit 1
fi
if grep -Fq -- 'fake-runtime-auth-only' "$AUTH_ONLY_LOG" ||
   grep -Fq -- 'fake-runtime-auth-only' "$DOCKER_ACTION_LOG"; then
  echo "AUTH_TOKEN fallback credential was exposed in Docker argv" >&2
  exit 1
fi
assert_env_files_removed "AUTH_TOKEN fallback DinD run"

: > "$DOCKER_ENV_CAPTURE_LOG"
FAILURE_LOG="$TMP_DIR/failure.log"
if PATH="$TMP_DIR/bin:$PATH" \
  MOCK_FAIL_RUN_FLEET=1 \
  DIND_BOOTSTRAP=always \
  TRACE_TO_OPIK=false \
  "$PROJECT_DIR/scripts/dind-run.sh" \
    --taskset terminalbench21 --agent claude-code --workers 1 \
    > "$FAILURE_LOG" 2>&1; then
  echo "dind-run.sh ignored a benchmark docker exec failure" >&2
  exit 1
fi
assert_env_files_removed "failed DinD run"
for secret in sk-local opik-local; do
  if grep -Fq -- "$secret" "$FAILURE_LOG" || grep -Fq -- "$secret" "$DOCKER_ACTION_LOG"; then
    echo "failed DinD run exposed a fake credential in Docker argv: $secret" >&2
    exit 1
  fi
done

run_signal_test() {
  local signal="$1" expected_status="$2"
  local signal_log="$TMP_DIR/signal-${signal}.log"
  local signal_status=0

  : > "$DOCKER_ENV_CAPTURE_LOG"
  : > "$DOCKER_SIGNAL_LOG"
  : > "$DOCKER_SIGNAL_HELPER_LOG"
  PATH="$TMP_DIR/bin:$PATH" \
  MOCK_SIGNAL_RUN_FLEET="$signal" \
  DIND_BOOTSTRAP=always \
  TRACE_TO_OPIK=false \
  "$PROJECT_DIR/scripts/dind-run.sh" \
    --taskset terminalbench21 --agent claude-code --workers 1 \
    > "$signal_log" 2>&1 ||
    signal_status=$?
  if [[ "$signal_status" != "$expected_status" ]]; then
    echo "dind-run.sh did not preserve the $signal exit status: $signal_status" >&2
    exit 1
  fi
  if ! grep -Fxq -- "$signal env_file=removed" "$DOCKER_SIGNAL_LOG"; then
    echo "dind-run.sh did not remove the env file before forwarding $signal" >&2
    exit 1
  fi
  if ! grep -Fxq -- \
    "$signal env_file=removed" "$DOCKER_SIGNAL_HELPER_LOG"; then
    echo "dind-run.sh did not signal the container process after env cleanup" >&2
    exit 1
  fi
  assert_env_files_removed "$signal-signaled DinD run"
}

run_signal_test TERM 143
run_signal_test INT 130

mkdir -p "$TMP_DIR/existing-bin"
cat > "$TMP_DIR/existing-bin/docker" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "ps" ]]; then
  printf '%s\n' 'agent-fleet-dind'
  exit 0
fi
if [[ "${1:-}" == "inspect" && "$*" == *"agent-fleet.runner-image"* ]]; then
  printf '%s\n' 'agent-fleet-dind:stale'
  exit 0
fi
exit 0
MOCK
chmod +x "$TMP_DIR/existing-bin/docker"

STALE_LOG="$TMP_DIR/stale.log"
if PATH="$TMP_DIR/existing-bin:$PATH" \
  DIND_IMAGE=agent-fleet-dind:current \
  "$PROJECT_DIR/scripts/dind-run.sh" \
    --taskset terminalbench21 --agent claude-code --workers 1 \
    > "$STALE_LOG" 2>&1; then
  echo "dind-run.sh reused a container created from a stale runner image" >&2
  exit 1
fi
grep -q -- 'uses a different runner image' "$STALE_LOG"
grep -q -- 'rerun with DIND_RECREATE=1' "$STALE_LOG"

RECREATE_LOG="$TMP_DIR/recreate.log"
: > "$DOCKER_ACTION_LOG"
PATH="$TMP_DIR/bin:$PATH" \
DIND_RECREATE=1 \
DIND_BOOTSTRAP=skip \
"$PROJECT_DIR/scripts/dind-run.sh" \
  --taskset terminalbench21 --agent claude-code --workers 1 \
  > "$RECREATE_LOG"
grep -q -- '^rm -f agent-fleet-dind$' "$DOCKER_ACTION_LOG"
if grep -q -- '^volume rm ' "$DOCKER_ACTION_LOG"; then
  echo "DIND_RECREATE removed the Docker storage volume" >&2
  exit 1
fi

PATH="$TMP_DIR/bin:$PATH" \
DIND_BOOTSTRAP=missing \
PI_VERSION=0.81.1 \
"$PROJECT_DIR/scripts/dind-run.sh" --taskset terminalbench21 --agent claude-code --workers 1 > "$LOG"

grep -qF -- '<sh> <-c> <paths_file="${AGENT_FLEET_PATHS_FILE:-${XDG_CONFIG_HOME:-$4/.config}/agent-fleet/paths.env}"; command -v pi >/dev/null 2>&1 && [ "$(pi --version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -n 1)" = "$3" ] && test -f "$1" && test -d "$2" && test -f "$paths_file">' "$LOG"
grep -q -- '</home/agent/.pi/agent/models.json>' "$LOG"
grep -q -- '</home/agent/.pi/agent/skills/harbor-benchmark-runner>' "$LOG"
grep -q -- '<0.81.1> </home/agent>' "$LOG"
grep -q -- '^ENV PI_VERSION=0.81.1$' "$DOCKER_ENV_CAPTURE_LOG"
if grep -q -- 'command -v claude' "$LOG"; then
  echo "dind-run.sh still checks the controller for Claude Code" >&2
  exit 1
fi
if grep -q -- '<./scripts/setup.sh>' "$LOG"; then
  echo "dind-run.sh bootstrapped despite a complete Pi controller setup" >&2
  exit 1
fi
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$LOG"

MISSING_PATHS_LOG="$TMP_DIR/missing-paths.log"
PATH="$TMP_DIR/bin:$PATH" \
MOCK_PATHS_ENV_MISSING=1 \
DIND_BOOTSTRAP=missing \
PI_VERSION=0.81.1 \
"$PROJECT_DIR/scripts/dind-run.sh" \
  --taskset terminalbench21 --agent claude-code --workers 1 \
  > "$MISSING_PATHS_LOG"

grep -q -- 'AGENT_FLEET_PATHS_FILE' "$MISSING_PATHS_LOG"
grep -q -- '<./scripts/setup.sh>' "$MISSING_PATHS_LOG"
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$MISSING_PATHS_LOG"

for paths_source in AGENT_FLEET_PATHS_FILE XDG_CONFIG_HOME; do
  CONFIGURED_PATHS_LOG="$TMP_DIR/configured-paths-${paths_source}.log"
  PATH="$TMP_DIR/bin:$PATH" \
  MOCK_CONTAINER_PATHS_SOURCE="$paths_source" \
  DIND_BOOTSTRAP=missing \
  PI_VERSION=0.81.1 \
  "$PROJECT_DIR/scripts/dind-run.sh" \
    --taskset terminalbench21 --agent claude-code --workers 1 \
    > "$CONFIGURED_PATHS_LOG"

  if grep -q -- '<./scripts/setup.sh>' "$CONFIGURED_PATHS_LOG"; then
    echo "dind-run.sh ignored the container's configured paths-file source: $paths_source" >&2
    exit 1
  fi
done

FALLBACK_LOG="$TMP_DIR/fallback.log"
PATH="$TMP_DIR/bin:$PATH" \
container=docker \
DIND_TEST_ASSUME_HOST=0 \
"$PROJECT_DIR/scripts/dind-run.sh" --taskset terminalbench21 --agent claude-code --workers 1 --dry-run > "$FALLBACK_LOG" 2>&1

grep -q -- '\[WARN\] dind-run.sh cannot start DinD inside a container; running scripts/run_fleet.sh directly' "$FALLBACK_LOG"
grep -q -- 'Command: env DATASET_NAME=terminalbench21 AGENT=claude-code TB_AGENT=claude-code TOTAL_WORKERS=1 TB_N_CONCURRENT=1 FLEET_TASKS= bash' "$FALLBACK_LOG"
if grep -q '^docker' "$FALLBACK_LOG"; then
  echo "dind-run.sh invoked Docker after detecting a container" >&2
  exit 1
fi

PATH="$TMP_DIR/bin:$PATH" \
DIND_BOOTSTRAP=always \
DIND_REGISTRY_MIRRORS=https://override-mirror.invalid \
DIND_DEFAULT_ADDRESS_POOLS=base=10.50.0.0/16,size=21 \
"$PROJECT_DIR/scripts/dind-run.sh" --taskset terminalbench21 --agent claude-code --workers 1 > "$LOG"

grep -q -- '--registry-mirror=https://override-mirror.invalid' "$LOG"
grep -q -- '--default-address-pool=base=10.50.0.0/16,size=21' "$LOG"
if grep -q -- '--registry-mirror=https://docker.m.daocloud.io' "$LOG"; then
  echo "caller DIND_REGISTRY_MIRRORS did not override config.local.env" >&2
  exit 1
fi
if grep -q -- '--default-address-pool=base=10.200.0.0/13,size=21' "$LOG"; then
  echo "caller DIND_DEFAULT_ADDRESS_POOLS did not override config.local.env" >&2
  exit 1
fi
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$LOG"
