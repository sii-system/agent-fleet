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
cp "$REPO_ROOT/scripts/run_fleet.sh" "$PROJECT_DIR/scripts/run_fleet.sh"
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
DIND_REGISTRY_MIRRORS="https://docker.m.daocloud.io, https://mirror.ccs.tencentyun.com"
DIND_DEFAULT_ADDRESS_POOLS="base=10.200.0.0/13,size=21;base=172.16.0.0/12,size=20"
EOF

LOG="$TMP_DIR/docker.log"
DOCKER_ACTION_LOG="$TMP_DIR/docker-actions.log"
export DOCKER_ACTION_LOG
cat > "$TMP_DIR/bin/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_ACTION_LOG"
printf 'docker'
for arg in "$@"; do
  printf ' <%s>' "$arg"
done
printf '\n'

if [[ "${1:-}" == "ps" ]]; then
  exit 0
fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  exit 1
fi
if [[ "${1:-}" == "exec" && "$*" == *"docker info"* ]]; then
  exit 0
fi
exit 0
MOCK
chmod +x "$TMP_DIR/bin/docker"

PATH="$TMP_DIR/bin:$PATH" \
DIND_BOOTSTRAP=always \
HTTP_PROXY=http://proxy.invalid:8080 \
HTTPS_PROXY=http://proxy.invalid:8443 \
NO_PROXY=existing.example \
TRACE_TO_OPIK=false \
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
grep -q -- '<env> <REPO_DIR='"$PROJECT_DIR"'> <BASE_URL=https://local.example.com> <API_KEY=sk-local> <MODEL=local-model>' "$LOG"
# The documented no-Opik escape must survive the DinD env handoff.
grep -q -- '<TRACE_TO_OPIK=false>' "$LOG"
if grep -q -- '<sh> <-lc>.*apk add' "$LOG"; then
  echo "dind-run.sh installed dependencies inside the running DinD container" >&2
  exit 1
fi
grep -q -- '<./scripts/setup.sh>' "$LOG"
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$LOG"

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

grep -qF -- '<sh> <-c> <command -v pi >/dev/null 2>&1 && [ "$(pi --version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -n 1)" = "$3" ] && test -f "$1" && test -d "$2">' "$LOG"
grep -q -- '</home/agent/.pi/agent/models.json>' "$LOG"
grep -q -- '</home/agent/.pi/agent/skills/harbor-benchmark-runner>' "$LOG"
grep -q -- '<0.81.1>' "$LOG"
grep -q -- '<PI_VERSION=0.81.1>' "$LOG"
if grep -q -- 'command -v claude' "$LOG"; then
  echo "dind-run.sh still checks the controller for Claude Code" >&2
  exit 1
fi
if grep -q -- '<./scripts/setup.sh>' "$LOG"; then
  echo "dind-run.sh bootstrapped despite a complete Pi controller setup" >&2
  exit 1
fi
grep -q -- '<./scripts/run_fleet.sh> <--taskset> <terminalbench21> <--agent> <claude-code> <--workers> <1>' "$LOG"

FALLBACK_LOG="$TMP_DIR/fallback.log"
PATH="$TMP_DIR/bin:$PATH" \
container=docker \
DIND_TEST_ASSUME_HOST=0 \
"$PROJECT_DIR/scripts/dind-run.sh" --taskset terminalbench21 --agent claude-code --workers 1 --dry-run > "$FALLBACK_LOG" 2>&1

grep -q -- '\[WARN\] dind-run.sh cannot start DinD inside a container; running scripts/run_fleet.sh directly' "$FALLBACK_LOG"
grep -q -- 'Command: env DATASET_NAME=terminalbench21 AGENT=claude-code TB_AGENT=claude-code TOTAL_WORKERS=1 TB_N_CONCURRENT=1 bash' "$FALLBACK_LOG"
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
