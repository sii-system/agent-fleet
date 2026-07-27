#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PROJECT_DIR="$TMP_DIR/Agents/Openclaw"
mkdir -p "$PROJECT_DIR/scripts" \
         "$PROJECT_DIR/cache/openclaw/.git" \
         "$TMP_DIR/third_party/agent-opik-plugin/harness/openclaw" \
         "$TMP_DIR/third_party/agent-opik-plugin/src/sii_opik_plugin/openclaw" \
         "$TMP_DIR/bin"
touch "$TMP_DIR/third_party/agent-opik-plugin/src/sii_opik_plugin/openclaw/openclaw_opik_tracer.py"
touch "$TMP_DIR/third_party/agent-opik-plugin/requirements.txt"
printf '{"scripts":{"build":"true"}}\n' > "$TMP_DIR/third_party/agent-opik-plugin/harness/openclaw/package.json"

cp "$OPENCLAW_DIR/scripts/build-openclaw-image.sh" "$PROJECT_DIR/scripts/build-openclaw-image.sh"
cp "$OPENCLAW_DIR/Dockerfile.opik" "$PROJECT_DIR/Dockerfile.opik"
chmod +x "$PROJECT_DIR/scripts/build-openclaw-image.sh"

LOG="$TMP_DIR/commands.log"

cat > "$TMP_DIR/bin/git" <<'MOCK'
#!/usr/bin/env bash
printf 'git %s API_KEY=%s OPIK_API_KEY=%s\n' \
  "$*" "${API_KEY:-}" "${OPIK_API_KEY:-}" >> "$LOG"
exit 0
MOCK

cat > "$TMP_DIR/bin/docker" <<'MOCK'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "$LOG"
exit 0
MOCK

cat > "$TMP_DIR/bin/npm" <<'MOCK'
#!/usr/bin/env bash
printf 'npm %s NPM_CONFIG_REGISTRY=%s\n' "$*" "${NPM_CONFIG_REGISTRY:-}" >> "$LOG"
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
  mkdir -p dist
  : > dist/index.js
fi
exit 0
MOCK

chmod +x "$TMP_DIR/bin/git" "$TMP_DIR/bin/docker" "$TMP_DIR/bin/npm"

PATH="$TMP_DIR/bin:$PATH" \
LOG="$LOG" \
OPIK_PLUGIN=enabled \
NPM_CONFIG_REGISTRY="https://registry.npmmirror.com" \
PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple" \
PIP_EXTRA_INDEX_URL="https://pypi.example.com/simple" \
PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn" \
"$PROJECT_DIR/scripts/build-openclaw-image.sh" >/dev/null

grep -q 'registry=https://registry.npmmirror.com' "$PROJECT_DIR/cache/openclaw/.npmrc"
grep -q -- '--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple' "$LOG"
grep -q -- '--build-arg PIP_EXTRA_INDEX_URL=https://pypi.example.com/simple' "$LOG"
grep -q -- '--build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn' "$LOG"
grep -q -- '--build-arg NPM_CONFIG_REGISTRY=https://registry.npmmirror.com' "$LOG"

# The fleet-wide switch wins over stale subsystem configuration and must not
# require an Opik plugin checkout or build the derived image.
: > "$LOG"
PATH="$TMP_DIR/bin:$PATH" \
LOG="$LOG" \
TRACE_TO_OPIK=false \
OPIK_PLUGIN=enabled \
TRACE_PLUGIN_SOURCE_DIR="$TMP_DIR/missing-opik-plugin" \
"$PROJECT_DIR/scripts/build-openclaw-image.sh" >/dev/null 2>&1

grep -q -- 'build --load -t openclaw:local ' "$LOG"
if grep -q -- 'openclaw:local-opik' "$LOG"; then
  echo "trace-off unexpectedly built the Opik image" >&2
  exit 1
fi

# A TRACE_TO_OPIK value persisted in the shared config is authoritative even
# when the caller's shell still exports a stale OPIK_PLUGIN value.
printf 'TRACE_TO_OPIK=false\nAPI_KEY=config-model-secret\nOPIK_API_KEY=config-opik-secret\n' \
  > "$TMP_DIR/config.local.env"
: > "$LOG"
env -u TRACE_TO_OPIK -u API_KEY -u OPIK_API_KEY \
  PATH="$TMP_DIR/bin:$PATH" \
  LOG="$LOG" \
  OPIK_PLUGIN=enabled \
  TRACE_PLUGIN_SOURCE_DIR="$TMP_DIR/missing-opik-plugin" \
  "$PROJECT_DIR/scripts/build-openclaw-image.sh" >/dev/null 2>&1

grep -q -- 'build --load -t openclaw:local ' "$LOG"
if grep -q -- 'openclaw:local-opik' "$LOG"; then
  echo "config-file trace-off unexpectedly built the Opik image" >&2
  exit 1
fi
if grep -q -- 'config-model-secret\|config-opik-secret' "$LOG"; then
  echo "config-file secrets leaked into build child environments" >&2
  exit 1
fi

# An explicit one-off caller value remains the highest-precedence layer.
: > "$LOG"
PATH="$TMP_DIR/bin:$PATH" \
LOG="$LOG" \
TRACE_TO_OPIK=true \
OPIK_PLUGIN=enabled \
"$PROJECT_DIR/scripts/build-openclaw-image.sh" >/dev/null

grep -q -- 'openclaw:local-opik' "$LOG"
