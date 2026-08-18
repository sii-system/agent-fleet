#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGER_PYTHON="${HARBOR_OPIK_PYTHON:-$(command -v python3)}"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/dataset/0/environment" "$tmp/deps/wheels" "$tmp/home"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uv"
chmod +x "$tmp/bin/uv"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uvx"
chmod +x "$tmp/bin/uvx"
printf '#!/usr/bin/env bash\necho "ELF 64-bit executable"\n' > "$tmp/bin/file"
chmod +x "$tmp/bin/file"
cat >"$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "info" ]]; then
  exit 0
fi
exit 0
SH
chmod +x "$tmp/bin/docker"
cat >"$tmp/bin/fake-harbor" <<'SH'
#!/usr/bin/env bash
printf 'FAKE_HARBOR_ARG=%s\n' "$@"
SH
chmod +x "$tmp/bin/fake-harbor"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/0/task.toml"
printf 'FROM ubuntu:24.04\n' > "$tmp/dataset/0/environment/Dockerfile"
printf 'fake package\n' > "$tmp/deps/claude.tgz"
printf 'fake wheel\n' > "$tmp/deps/wheels/dependency.whl"

run_dry() {
  local image_ref="$1"
  local manager="$2"
  local build_args_json="${3:-}"
  local dataset_name="${4:-auto}"
  local environment_type="${5:-opensandbox}"
  local force_build="${6:-0}"
  local agent="${7:-oracle}"
  local dry_run="${8:-1}"
  local runtime_dir="$tmp/runtime/$agent"
  local queue_dir="$tmp/queue/$agent"
  local harbor_python="$MANAGER_PYTHON"
  if [[ "$agent" == "opencode" && "$dry_run" == "0" ]]; then
    harbor_python="$tmp/bin/fake-harbor"
  fi
  mkdir -p "$queue_dir" "$runtime_dir"
  if [[ -z "$build_args_json" ]]; then
    build_args_json='{}'
  fi
  env -i \
    PATH="$tmp/bin:/usr/bin:/bin" \
    HOME="$tmp/home" \
    AGENT="$agent" \
    DATASET_NAME="$dataset_name" \
    DATASET_PATH="$tmp/dataset" \
    INCLUDE_TASKS=0 \
    OUTPUT_PATH="$tmp/output" \
    QUEUE_DIR="$queue_dir" \
    RUNTIME_DIR="$runtime_dir" \
    HARBOR_DRY_RUN="$dry_run" \
    HARBOR_FORCE_BUILD="$force_build" \
    HARBOR_N_CONCURRENT=1 \
    HARBOR_MAX_RETRIES=0 \
    API_KEY=fake-api-key \
    BASE_URL=https://model.example \
    MODEL=test-model \
    HARBOR_ANTHROPIC_AUTH_TOKEN=fake-api-key \
    HARBOR_LLM_KWARGS='{"temperature":1.0}' \
    HARBOR_CC_CLAUDE_TGZ_SOURCE="$tmp/deps/claude.tgz" \
    HARBOR_CC_PY_WHEEL_DIR_SOURCE="$tmp/deps/wheels" \
    TRACE_TO_OPIK=false \
    HARBOR_ENVIRONMENT_TYPE="$environment_type" \
    HARBOR_OPENSANDBOX_IMAGE_REF="$image_ref" \
    HARBOR_OPENSANDBOX_IMAGE_REPOSITORY=test-project/test-repository \
    HARBOR_OPENSANDBOX_IMAGE_MANAGER="$manager" \
    HARBOR_OPIK_BIN="$tmp/bin/fake-harbor" \
    HARBOR_CLI_BIN="$tmp/bin/fake-harbor" \
    HARBOR_RUNNER_PREPARE=0 \
    HARBOR_OPIK_PYTHON="$harbor_python" \
    HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="$build_args_json" \
    YICLOUD_PUBLIC_KEY=fake-public \
    YICLOUD_SECRET_KEY=fake-secret \
    YICLOUD_PROJECT_NAME=test-project \
    YICLOUD_SANDBOX_ENVIRONMENT_ID=env-test \
    YICLOUD_SANDBOX_ENVIRONMENT_NAME=test-environment \
    bash "$HARBOR_DIR/harboropik.sh" 2>&1
}

automatic="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}')"
grep -F -- '--env yicloud_opensandbox:YiCloudOpenSandboxEnvironment' <<< "$automatic" >/dev/null
grep -E -- '--ek image_ref=test-project/test-repository:harbor-0-[0-9a-f]{20}' \
  <<< "$automatic" >/dev/null
grep -F -- '--ek lifecycle_minutes=120' <<< "$automatic" >/dev/null
grep -F -- '--mounts-json' <<< "$automatic" >/dev/null
grep -F -- 'HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$automatic" >/dev/null
if grep -F -- '--extra-docker-compose' <<< "$automatic" >/dev/null; then
  echo 'OpenSandbox command unexpectedly contains a Docker compose overlay' >&2
  exit 1
fi
if [[ "$(grep -oF -- '--env yicloud_opensandbox:YiCloudOpenSandboxEnvironment' \
  <<< "$automatic" | wc -l | tr -d ' ')" != "1" ]]; then
  echo 'OpenSandbox command must contain exactly one YiCloud environment argument' >&2
  exit 1
fi

for force_build in 1 true; do
  forced="$(run_dry \
    '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}' auto opensandbox \
    "$force_build")"
  grep -E -- \
    '--ek image_ref=test-project/test-repository:harbor-0-[0-9a-f]{20}-r[0-9]{14}-[0-9]+' \
    <<< "$forced" >/dev/null
done

docker_run="$(run_dry '' "$tmp/does-not-exist.py" '{}' auto docker)"
grep -F -- \
  "--extra-docker-compose $HARBOR_DIR/overlays/unprivileged-task.yaml" \
  <<< "$docker_run" >/dev/null
if grep -F -- '--ek image_ref=' <<< "$docker_run" >/dev/null; then
  echo 'Docker command unexpectedly contains OpenSandbox image arguments' >&2
  exit 1
fi

automatic_seta="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}' seta)"
grep -E -- '--ek image_ref=test-project/test-repository:harbor-0-[0-9a-f]{20}' \
  <<< "$automatic_seta" >/dev/null
grep -F -- "--path $tmp/dataset" <<< "$automatic_seta" >/dev/null
if grep -F -- '--dataset seta-env' <<< "$automatic_seta" >/dev/null; then
  echo 'OpenSandbox local SETA path unexpectedly resolved through Harbor Registry' >&2
  exit 1
fi

manual="$(run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py")"
grep -F -- '--ek image_ref=test-project/manual:immutable' <<< "$manual" >/dev/null
if grep -F -- '[INFO] preparing OpenSandbox image' <<< "$manual" >/dev/null; then
  echo 'manual image override unexpectedly invoked the image manager' >&2
  exit 1
fi

claude_opensandbox="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' auto \
  opensandbox 0 claude-code 0)"
grep -F -- 'FAKE_HARBOR_ARG=--mounts-json' <<< "$claude_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_CLAUDE_TGZ_PATH=' \
  <<< "$claude_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$claude_opensandbox" >/dev/null

opencode_opensandbox="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' auto \
  opensandbox 0 opencode 0)"
grep -F -- 'FAKE_HARBOR_ARG=--mounts-json' <<< "$opencode_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=OPENCODE_TGZ_PATH=' \
  <<< "$opencode_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$opencode_opensandbox" >/dev/null
