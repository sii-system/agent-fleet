#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
verifier_path='PATH=/root/.local/bin:/home/oai/.local/bin:/home/agent/.local/bin:/home/ubuntu/.local/bin:/opt/tb-uv-backup/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
upstream_node_url='https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.gz'

mkdir -p "$tmp/bin" "$tmp/dataset/0/environment" "$tmp/home" "$tmp/queue" "$tmp/runtime"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uv"
chmod +x "$tmp/bin/uv"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uvx"
chmod +x "$tmp/bin/uvx"
printf '#!/usr/bin/env bash\necho "ELF 64-bit executable"\n' > "$tmp/bin/file"
chmod +x "$tmp/bin/file"
printf '#!/usr/bin/env bash\nif [[ "${1:-}" == "-s" ]]; then echo Linux; else /usr/bin/uname "$@"; fi\n' > "$tmp/bin/uname"
chmod +x "$tmp/bin/uname"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@"\n' > "$tmp/bin/opik"
chmod +x "$tmp/bin/opik"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/harbor"
chmod +x "$tmp/bin/harbor"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/0/task.toml"
printf 'FROM ubuntu:24.04\n' > "$tmp/dataset/0/environment/Dockerfile"
printf '{"identity_version":"qz-template-image-v1","schema_version":1,"tasks":{},"templates":{}}\n' > "$tmp/qz-map.json"

run_dry() {
  local sbx_api_key="$1"
  local qz_template="$2"
  local qz_timeout="${3:-}"
  local agent="${4:-oracle}"
  local force_build="${5:-0}"
  local e2b_api_key="${6:-}"
  local dry_run="${7:-1}"
  local npm_registry="${8:-}"
  local qz_node_dist_url="${9:-}"
  local qz_template_map="${10:-}"
  env -i \
    AGENT="$agent" \
    HARBOR_FORCE_BUILD="$force_build" \
    QZ_SANDBOX_TIMEOUT_SEC="$qz_timeout" \
    HARBOR_ANTHROPIC_BASE_URL=http://fake-gw \
    HARBOR_ANTHROPIC_AUTH_TOKEN=fake_token \
    PATH="$tmp/bin:/usr/bin:/bin" \
    HOME="$tmp/home" \
    DATASET_NAME=auto \
    DATASET_PATH="$tmp/dataset" \
    INCLUDE_TASKS=0 \
    OUTPUT_PATH="$tmp/output" \
    QUEUE_DIR="$tmp/queue" \
    RUNTIME_DIR="$tmp/runtime" \
    HARBOR_DRY_RUN="$dry_run" \
    HARBOR_N_CONCURRENT=1 \
    HARBOR_MAX_RETRIES=0 \
    HARBOR_RUNNER_PREPARE=0 \
    HARBOR_OPIK_BIN="$tmp/bin/opik" \
    HARBOR_CLI_BIN="$tmp/bin/harbor" \
    HARBOR_OPIK_PYTHON="$tmp/bin/harbor" \
    HARBOR_ENVIRONMENT_TYPE=qz \
    SBX_API_KEY="$sbx_api_key" \
    E2B_API_KEY="$e2b_api_key" \
    QZ_SANDBOX_TEMPLATE="$qz_template" \
    QZ_SANDBOX_TEMPLATE_MAP="$qz_template_map" \
    NPM_CONFIG_REGISTRY="$npm_registry" \
    QZ_NODE_DIST_URL="$qz_node_dist_url" \
    bash "$HARBOR_DIR/harboropik.sh" 2>&1
}

# A configured qz run passes the adapter import path and nothing docker- or
# opensandbox-specific.
qz_run="$(run_dry sbx_fake_key fake_template)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' <<< "$qz_run" >/dev/null
if [[ "$(grep -oF -- '--env qz_e2b_sandbox:QzSandboxEnvironment' \
  <<< "$qz_run" | wc -l | tr -d ' ')" != "1" ]]; then
  echo 'qz command must contain exactly one qz environment argument' >&2
  exit 1
fi
if grep -F -- '--extra-docker-compose' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains a Docker compose overlay' >&2
  exit 1
fi
if grep -F -- '--ek image_ref=' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains OpenSandbox image arguments' >&2
  exit 1
fi
if grep -F -- '--mounts-json' <<< "$qz_run" >/dev/null; then
  echo 'qz command unexpectedly contains host bind mounts' >&2
  exit 1
fi

# A per-task mapping is an alternative to the fixed Template mode.
mapping_run="$(run_dry sbx_fake_key '' '' oracle 0 '' 1 '' '' "$tmp/qz-map.json")"
grep -F -- "qz sandbox template map: $tmp/qz-map.json" <<< "$mapping_run" >/dev/null

# Pi requires host bind mounts and must fail in the reachable qz validation
# branch before Harbor starts.
if pi_run="$(run_dry sbx_fake_key fake_template '' pi)"; then
  echo 'qz launch unexpectedly accepted AGENT=pi' >&2
  exit 1
else
  grep -F -- 'AGENT=pi with HARBOR_ENVIRONMENT_TYPE=qz is unsupported' \
    <<< "$pi_run" >/dev/null
fi

# A missing key must fail launch validation before Harbor runs.
if missing_key="$(run_dry '' fake_template)"; then
  echo 'qz launch unexpectedly succeeded without an API key' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires SBX_API_KEY' <<< "$missing_key" >/dev/null
fi

# A legacy E2B_API_KEY is accepted only when it is visibly a qz sbx_ key. An
# ambient cloud-E2B key must fail before Harbor can send it to the qz endpoint.
legacy_key_run="$(run_dry '' fake_template '' oracle 0 sbx_legacy_key)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' \
  <<< "$legacy_key_run" >/dev/null
if cloud_key_run="$(run_dry '' fake_template '' oracle 0 e2b_cloud_key)"; then
  echo 'qz launch unexpectedly accepted an ambient cloud-E2B API key' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires SBX_API_KEY' <<< "$cloud_key_run" >/dev/null
fi

# A missing template must fail launch validation before Harbor runs.
if missing_template="$(run_dry sbx_fake_key '')"; then
  echo 'qz launch unexpectedly succeeded without a template' >&2
  exit 1
else
  grep -F -- 'qz sandbox requires QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP' <<< "$missing_template" >/dev/null
fi

# The two modes are deliberately exclusive and mapping paths fail early.
if conflicting="$(run_dry sbx_fake_key fake_template '' oracle 0 '' 1 '' '' "$tmp/qz-map.json")"; then
  echo 'qz launch unexpectedly accepted both Template selection modes' >&2
  exit 1
else
  grep -F -- 'set only one of QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP' <<< "$conflicting" >/dev/null
fi
if missing_map="$(run_dry sbx_fake_key '' '' oracle 0 '' 1 '' '' "$tmp/missing.json")"; then
  echo 'qz launch unexpectedly accepted a missing Template mapping' >&2
  exit 1
else
  grep -F -- 'QZ_SANDBOX_TEMPLATE_MAP not found' <<< "$missing_map" >/dev/null
fi

# An invalid timeout must fail launch validation before Harbor runs.
for bad_timeout in abc 0 14401; do
  if bad_run="$(run_dry sbx_fake_key fake_template "$bad_timeout")"; then
    echo "qz launch unexpectedly succeeded with QZ_SANDBOX_TIMEOUT_SEC=$bad_timeout" >&2
    exit 1
  else
    grep -F -- 'QZ_SANDBOX_TIMEOUT_SEC must be an integer between 1 and 14400' \
      <<< "$bad_run" >/dev/null
  fi
done

# A valid timeout passes through.
valid_run="$(run_dry sbx_fake_key fake_template 600)"
grep -F -- '--env qz_e2b_sandbox:QzSandboxEnvironment' <<< "$valid_run" >/dev/null

# claude-code passes validation and uses the regional runtime-source defaults,
# still without Notebook-host bind mounts.
cc_run="$(run_dry sbx_fake_key fake_template '' claude-code 0 '' 0)"
grep -F -- 'qz claude-code delivery: npm registry https://registry.npmmirror.com' \
  <<< "$cc_run" >/dev/null
grep -F -- 'node dist https://registry.npmmirror.com/-/binary/node/' \
  <<< "$cc_run" >/dev/null
if grep -F -- '--mounts-json' <<< "$cc_run" >/dev/null; then
  echo 'qz claude-code command unexpectedly contains host bind mounts' >&2
  exit 1
fi
grep -F -- 'HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' <<< "$cc_run" >/dev/null
grep -F -- "$verifier_path" <<< "$cc_run" >/dev/null

# opencode uses the same runtime-source defaults and must not depend on
# runner-local package URLs or Notebook-host bind mounts.
oc_run="$(run_dry sbx_fake_key fake_template '' opencode)"
grep -F -- 'qz opencode delivery: npm registry https://registry.npmmirror.com' \
  <<< "$oc_run" >/dev/null
grep -F -- 'node dist https://registry.npmmirror.com/-/binary/node/' \
  <<< "$oc_run" >/dev/null
grep -F -- '--agent-import-path' <<< "$oc_run" >/dev/null
grep -F -- 'opik_opencode_harbor:OpikOpenCodeHarbor' <<< "$oc_run" >/dev/null
grep -F -- 'CC_NODE_DIST_URL=https://registry.npmmirror.com/-/binary/node/' \
  <<< "$oc_run" >/dev/null
if grep -F -- '--mounts-json' <<< "$oc_run" >/dev/null; then
  echo 'qz opencode command unexpectedly contains host bind mounts' >&2
  exit 1
fi
if grep -F -- 'HARBOR_LOCAL_OPENCODE_TGZ_URL=http' <<< "$oc_run" >/dev/null; then
  echo 'qz opencode command unexpectedly uses a runner-local package URL' >&2
  exit 1
fi
grep -F -- 'HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' <<< "$oc_run" >/dev/null
grep -F -- "$verifier_path" <<< "$oc_run" >/dev/null
if grep -F -- 'fake_token' <<< "$oc_run" >/dev/null; then
  echo 'qz opencode command unexpectedly contains the model API key' >&2
  exit 1
fi

# Direct opencode runs must initialize their own queue/runtime directories
# before local task selection, rather than relying on start.sh or a worker.
rm -rf -- "$tmp/queue" "$tmp/runtime"
direct_oc_run="$(run_dry sbx_fake_key fake_template '' opencode 0 '' 0)"
if [[ ! -f "$tmp/queue/next_index" ]]; then
  echo 'direct qz opencode run did not initialize queue/next_index' >&2
  exit 1
fi
if grep -F -- 'fake_token' <<< "$direct_oc_run" >/dev/null; then
  echo 'direct qz opencode run unexpectedly contains the model API key' >&2
  exit 1
fi

# Public upstream sources remain an explicit supported choice; npmmirror is a
# regional default rather than the only accepted route.
upstream_run="$(
  run_dry sbx_fake_key fake_template '' opencode 0 '' 1 \
    https://registry.npmjs.org "$upstream_node_url"
)"
grep -F -- 'qz opencode delivery: npm registry https://registry.npmjs.org' \
  <<< "$upstream_run" >/dev/null
grep -F -- "node dist $upstream_node_url" <<< "$upstream_run" >/dev/null
grep -F -- "CC_NODE_DIST_URL=$upstream_node_url" <<< "$upstream_run" >/dev/null

# The qz fallback must not turn npmmirror into a repository-wide runtime
# default. Non-qz Harbor backends preserve their explicit npmjs default.
non_qz_registry="$(
  env -i \
    PATH="$tmp/bin:/usr/bin:/bin" \
    HOME="$tmp/home" \
    AGENT_FLEET_PATHS_FILE="$tmp/no-paths" \
    AGENT_FLEET_RUNTIME_DIR="$tmp/prerequisite-runtime" \
    HARBOR_ENVIRONMENT_TYPE=docker \
    NPM_CONFIG_REGISTRY= \
    bash -c 'source "$1"; printf "%s" "$NPM_CONFIG_REGISTRY"' \
    _ "$HARBOR_DIR/env.sh"
)"
if [[ "$non_qz_registry" != "https://registry.npmjs.org" ]]; then
  echo "non-qz backend resolved unexpected npm registry: $non_qz_registry" >&2
  exit 1
fi

# force_build has no meaning for platform-registered templates.
for force_build in 1 true; do
  if fb_run="$(run_dry sbx_fake_key fake_template '' oracle "$force_build")"; then
    echo "qz launch unexpectedly succeeded with HARBOR_FORCE_BUILD=$force_build" >&2
    exit 1
  else
    grep -F -- 'HARBOR_FORCE_BUILD is not supported on qz' <<< "$fb_run" >/dev/null
  fi
done

echo 'test_harboropik_qz.sh passed'
