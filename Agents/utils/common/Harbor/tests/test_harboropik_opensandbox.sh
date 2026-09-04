#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGER_PYTHON="${HARBOR_OPIK_PYTHON:-$(command -v python3)}"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p \
  "$tmp/bin" \
  "$tmp/dataset/0/environment" \
  "$tmp/dataset/1/environment" \
  "$tmp/deps/wheels" \
  "$tmp/home"
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
cat >"$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == '%{http_code}' ]]; then
    printf '200'
  fi
done
exit 0
SH
chmod +x "$tmp/bin/curl"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/0/task.toml"
printf 'FROM ubuntu:24.04\n' > "$tmp/dataset/0/environment/Dockerfile"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/1/task.toml"
printf 'FROM alpine:3.20\n' > "$tmp/dataset/1/environment/Dockerfile"
printf 'fake package\n' > "$tmp/deps/claude.tgz"
printf 'fake wheel\n' > "$tmp/deps/wheels/dependency.whl"
printf '# fake Claude Opik hook\n' > "$tmp/deps/claude_realtime_trace.py"
mkdir -p "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin"
printf 'target-system-libraries\n' \
  > "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/.harbor-python-runtime-v2"
printf '#!/bin/sh\nexit 0\n' \
  > "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/python3.12"
printf '#!/bin/sh\nexit 0\n' \
  > "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/harbor-verifier-bundle-check"
chmod +x \
  "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/python3.12" \
  "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/harbor-verifier-bundle-check"
ln -s python3.12 "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/python3"
ln -s python3.12 "$tmp/verifier-bundle/agent-fleet-swe-rebench-v2-verifier-bundle/bin/python"
tar -czf "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz" \
  -C "$tmp/verifier-bundle" agent-fleet-swe-rebench-v2-verifier-bundle
printf '# fake Exa MCP\n' > "$tmp/deps/exa_web_mcp.py"

run_dry() {
  local image_ref="$1"
  local manager="$2"
  local build_args_json="${3:-}"
  local dataset_name="${4:-auto}"
  local environment_type="${5:-opensandbox}"
  local force_build="${6:-0}"
  local agent="${7:-oracle}"
  local dry_run="${8:-1}"
  local extension_source="${9:-$tmp/no-pi-extensions}"
  local bundle_manifest="${10:-}"
  local include_tasks="${11:-0}"
  local jobs_root="${12:-$tmp/jobs/$agent}"
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
    INCLUDE_TASKS="$include_tasks" \
    OUTPUT_PATH="$tmp/output" \
    QUEUE_DIR="$queue_dir" \
    RUNTIME_DIR="$runtime_dir" \
    JOBS_ROOT="$jobs_root" \
    HARBOR_DRY_RUN="$dry_run" \
    HARBOR_FORCE_BUILD="$force_build" \
    HARBOR_N_CONCURRENT=1 \
    HARBOR_MAX_RETRIES=0 \
    API_KEY=fake-api-key \
    BASE_URL=https://model.example \
    MODEL=test-model \
    OPIK_URL="${RUN_DRY_OPIK_URL:-}" \
    HARBOR_CC_OPIK_ENABLE_HOOK="${RUN_DRY_CC_OPIK_ENABLE_HOOK:-}" \
    HARBOR_CC_HOOK_SOURCE="${RUN_DRY_CC_HOOK_SOURCE:-$tmp/deps/claude_realtime_trace.py}" \
    HARBOR_ANTHROPIC_AUTH_TOKEN=fake-api-key \
    HARBOR_LLM_KWARGS='{"temperature":1.0}' \
    HARBOR_CC_CLAUDE_TGZ_SOURCE="$tmp/deps/claude.tgz" \
    HARBOR_CC_PY_WHEEL_DIR_SOURCE="$tmp/deps/wheels" \
    HARBOR_CC_WEB_MCP_SOURCE="${RUN_DRY_WEB_MCP_SOURCE:-}" \
    EXA_API_KEY="${RUN_DRY_EXA_API_KEY:-}" \
    HARBOR_ENVIRONMENT_TYPE="$environment_type" \
    YICLOUD_SANDBOX_UPLOAD_BACKEND="${RUN_DRY_UPLOAD_BACKEND:-auto}" \
    YICLOUD_SANDBOX_S3_PROFILE= \
    HARBOR_OPENSANDBOX_IMAGE_REF="$image_ref" \
    HARBOR_OPENSANDBOX_BUNDLE_MANIFEST="$bundle_manifest" \
    YICLOUD_HARBOR_HOST=harbor.example.internal \
    YICLOUD_HARBOR_PROJECT="${RUN_DRY_HARBOR_PROJECT-test-project}" \
    HARBOR_OPENSANDBOX_IMAGE_MANAGER="$manager" \
    HARBOR_OPIK_BIN="$tmp/bin/fake-harbor" \
    HARBOR_CLI_BIN="$tmp/bin/fake-harbor" \
    HARBOR_RUNNER_PREPARE=0 \
    HARBOR_OPIK_PYTHON="$harbor_python" \
    HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="$build_args_json" \
    PI_EXTENSION_SOURCE="$extension_source" \
    E2B_API_KEY=fake-e2b-key \
    YICLOUD_PUBLIC_KEY=fake-public \
    YICLOUD_SECRET_KEY=fake-secret \
    YICLOUD_PROJECT_NAME=test-project \
    YICLOUD_SANDBOX_ENVIRONMENT_ID=env-test \
    YICLOUD_SANDBOX_ENVIRONMENT_NAME=test-environment \
    bash "$HARBOR_DIR/harboropik.sh" 2>&1
}

automatic="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}')"
grep -F -- '--env yicloud_opensandbox:YiCloudOpenSandboxEnvironment' <<< "$automatic" >/dev/null
grep -E -- '--ek image_ref=harbor\.example\.internal/test-project/0@sha256:[0-9a-f]{64}' \
  <<< "$automatic" >/dev/null
grep -F -- '--ek lifecycle_minutes=120' <<< "$automatic" >/dev/null
grep -F -- '--mounts-json' <<< "$automatic" >/dev/null
grep -F -- 'HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$automatic" >/dev/null
grep -F -- 'HARBOR_VERIFIER_PATH_PREPEND=/root/.local/bin:' \
  <<< "$automatic" >/dev/null
if grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_' <<< "$automatic" >/dev/null; then
  echo 'non-Rebench OpenSandbox verifier unexpectedly receives a runtime bundle' >&2
  exit 1
fi
if grep -F -- 'FAKE_HARBOR_ARG=PATH=' <<< "$automatic" >/dev/null; then
  echo 'OpenSandbox verifier unexpectedly replaces the task image PATH' >&2
  exit 1
fi

rebench="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' \
  agent-fleet-swe-rebench-v2)"
grep -F -- '[INFO] OpenSandbox verifier runtime bundle: benchmark=agent-fleet-swe-rebench-v2 bundle=agent-fleet-swe-rebench-v2-verifier-bundle' \
  <<< "$rebench" >/dev/null
grep -F -- 'HARBOR_VERIFIER_PATH_PREPEND=/tmp/harbor-verifier-bundles/agent-fleet-swe-rebench-v2-verifier-bundle/bin:' \
  <<< "$rebench" >/dev/null
grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_ID=agent-fleet-swe-rebench-v2-verifier-bundle' \
  <<< "$rebench" >/dev/null
if grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_KIND=' <<< "$rebench" >/dev/null; then
  echo 'Rebench verifier runtime bundle unexpectedly exposes a kind' >&2
  exit 1
fi
grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE=/opt/tb-opik/python-wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz' \
  <<< "$rebench" >/dev/null
grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_ROOT=/tmp/harbor-verifier-bundles/agent-fleet-swe-rebench-v2-verifier-bundle' \
  <<< "$rebench" >/dev/null
grep -F -- "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz" \
  <<< "$rebench" >/dev/null
grep -F -- '/opt/tb-opik/python-wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz' \
  <<< "$rebench" >/dev/null
if grep -F -- 'FAKE_HARBOR_ARG=PATH=' <<< "$rebench" >/dev/null; then
  echo 'Rebench OpenSandbox verifier unexpectedly replaces the task image PATH' >&2
  exit 1
fi

rebench_agent="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' \
  agent-fleet-swe-rebench-v2 opensandbox 0 claude-code)"
grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE=/opt/tb-opik/python-wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz' \
  <<< "$rebench_agent" >/dev/null
grep -F -- "\"source\":\"$tmp/deps/wheels\",\"target\":\"/opt/tb-opik/python-wheels\",\"read_only\":true" \
  <<< "$rebench_agent" >/dev/null

set +e
http_rebench="$(RUN_DRY_UPLOAD_BACKEND=http run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' \
  agent-fleet-swe-rebench-v2)"
http_rebench_status="$?"
set -e
if [[ "$http_rebench_status" == "0" ]]; then
  echo 'Rebench verifier runtime bundle unexpectedly accepted HTTP-only transport' >&2
  exit 1
fi
grep -F -- 'requires YICLOUD_SANDBOX_UPLOAD_BACKEND=s3 or auto' \
  <<< "$http_rebench" >/dev/null

legacy_rebench_alias="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' \
  swe-rebench-v2)"
if grep -F -- 'HARBOR_VERIFIER_RUNTIME_BUNDLE_' \
  <<< "$legacy_rebench_alias" >/dev/null; then
  echo 'legacy Rebench alias unexpectedly selects a verifier runtime bundle' >&2
  exit 1
fi

mv "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz" \
  "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz.saved"
set +e
missing_rebench_runtime="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' \
  agent-fleet-swe-rebench-v2)"
missing_rebench_status="$?"
set -e
mv "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz.saved" \
  "$tmp/deps/wheels/agent-fleet-swe-rebench-v2-verifier-bundle.tar.gz"
if [[ "$missing_rebench_status" == "0" ]]; then
  echo 'Rebench OpenSandbox verifier unexpectedly accepted a missing runtime' >&2
  exit 1
fi
grep -F -- 'runtime bundle agent-fleet-swe-rebench-v2-verifier-bundle selected for agent-fleet-swe-rebench-v2, but its archive is missing' \
  <<< "$missing_rebench_runtime" >/dev/null

grep -F -- '[INFO] OpenSandbox Bundle Manifest ready:' <<< "$automatic" >/dev/null
automatic_bundle="$(sed -n \
  's/^\[INFO\] OpenSandbox Bundle Manifest ready: //p' \
  <<< "$automatic" | tail -n 1)"
[[ -f "$automatic_bundle" ]]
[[ "$automatic_bundle" == "$tmp/jobs/oracle/"*/opensandbox-bundle.json ]]
grep -F -- "--ek bundle_manifest_path=$automatic_bundle" <<< "$automatic" >/dev/null
"$MANAGER_PYTHON" -c '
import json, pathlib, sys
bundle = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert bundle["schema_version"] == 2
assert bundle["main"] == "main"
assert bundle["registry"]["repository"] == "test-project/0"
assert bundle["services"]["main"]["image"]["digest_ref"].startswith(
    "harbor.example.internal/test-project/0@sha256:"
)
' "$automatic_bundle"

concurrent_jobs_0="$tmp/jobs/concurrent/task-0"
concurrent_jobs_1="$tmp/jobs/concurrent/task-1"
run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}' auto \
  opensandbox 0 oracle 1 '' '' 0 "$concurrent_jobs_0" \
  > "$tmp/concurrent-0.out" &
concurrent_pid_0="$!"
run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}' auto \
  opensandbox 0 oracle 1 '' '' 1 "$concurrent_jobs_1" \
  > "$tmp/concurrent-1.out" &
concurrent_pid_1="$!"
wait "$concurrent_pid_0"
wait "$concurrent_pid_1"
concurrent_bundle_0="$(sed -n \
  's/^\[INFO\] OpenSandbox Bundle Manifest ready: //p' \
  "$tmp/concurrent-0.out" | tail -n 1)"
concurrent_bundle_1="$(sed -n \
  's/^\[INFO\] OpenSandbox Bundle Manifest ready: //p' \
  "$tmp/concurrent-1.out" | tail -n 1)"
[[ "$concurrent_bundle_0" == "$concurrent_jobs_0/"*/opensandbox-bundle.json ]]
[[ "$concurrent_bundle_1" == "$concurrent_jobs_1/"*/opensandbox-bundle.json ]]
[[ "$concurrent_bundle_0" != "$concurrent_bundle_1" ]]
"$MANAGER_PYTHON" -c '
import json, pathlib, sys
for path, task in zip(sys.argv[1:], ("0", "1"), strict=True):
    bundle = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert bundle["registry"]["repository"] == f"test-project/{task}"
' "$concurrent_bundle_0" "$concurrent_bundle_1"
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
    '--ek image_ref=harbor\.example\.internal/test-project/0@sha256:[0-9a-f]{64}' \
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
grep -E -- '--ek image_ref=harbor\.example\.internal/test-project/0@sha256:[0-9a-f]{64}' \
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

manual_bundle="$tmp/manual-bundle.json"
cat >"$manual_bundle" <<'JSON'
{
  "schema_version": 1,
  "main_service": "main",
  "services": {
    "main": {
      "image": {
        "sandbox_ref": "test-project/from-bundle:immutable"
      }
    }
  }
}
JSON
manual_bundle_run="$(run_dry \
  '' "$tmp/does-not-exist.py" '{}' auto opensandbox 0 oracle 1 \
  "$tmp/no-pi-extensions" "$manual_bundle")"
grep -F -- '--ek image_ref=test-project/from-bundle:immutable' \
  <<< "$manual_bundle_run" >/dev/null
grep -F -- '[INFO] using OpenSandbox Bundle Manifest:' \
  <<< "$manual_bundle_run" >/dev/null

printf '[environment]\nbuild_timeout_sec = 60\ndocker_image = "harbor-sandbox.example/tasks:prebuilt"\n' \
  > "$tmp/dataset/0/task.toml"
task_prebuilt="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py")"
grep -F -- '--ek image_ref=harbor-sandbox.example/tasks:prebuilt' \
  <<< "$task_prebuilt" >/dev/null
task_prebuilt_without_project="$(
  RUN_DRY_HARBOR_PROJECT='' \
    run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py"
)"
grep -F -- '--ek image_ref=harbor-sandbox.example/tasks:prebuilt' \
  <<< "$task_prebuilt_without_project" >/dev/null
if grep -F -- '[INFO] preparing OpenSandbox image' <<< "$task_prebuilt" >/dev/null; then
  echo 'task prebuilt image unexpectedly invoked the image manager' >&2
  exit 1
fi
if grep -F -- 'import sys, tomllib' "$HARBOR_DIR/harboropik.sh" >/dev/null; then
  echo 'OpenSandbox task image parser still requires Python 3.11 tomllib' >&2
  exit 1
fi
printf '[environment]\ndocker_image = "unterminated\n' \
  > "$tmp/dataset/0/task.toml"
if invalid_task_image="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py")"; then
  echo 'invalid task.toml unexpectedly produced an OpenSandbox command' >&2
  exit 1
fi
grep -F -- '[ERROR] failed to read OpenSandbox image from task.toml' \
  <<< "$invalid_task_image" >/dev/null
printf '[environment]\ndocker_image = "invalid image"\n' \
  > "$tmp/dataset/0/task.toml"
if whitespace_task_image="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py")"; then
  echo 'whitespace task image unexpectedly produced an OpenSandbox command' >&2
  exit 1
fi
grep -F -- '[ERROR] task docker_image must be a single image reference' \
  <<< "$whitespace_task_image" >/dev/null
printf '[environment]\nbuild_timeout_sec = 60\ndocker_image = "harbor-sandbox.example/tasks:prebuilt"\n' \
  > "$tmp/dataset/0/task.toml"

claude_opensandbox="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' auto \
  opensandbox 0 claude-code 0)"
grep -F -- 'FAKE_HARBOR_ARG=--mounts-json' <<< "$claude_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_CLAUDE_TGZ_PATH=' \
  <<< "$claude_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$claude_opensandbox" >/dev/null

claude_opensandbox_web_mcp="$(
  RUN_DRY_WEB_MCP_SOURCE="$tmp/deps/exa_web_mcp.py" \
  RUN_DRY_EXA_API_KEY='fake-exa-key' \
    run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py" \
      '{}' auto opensandbox 0 claude-code 0
)"
grep -F -- 'FAKE_HARBOR_ARG=CC_WEB_MCP_PATH=/opt/agent-fleet/exa_web_mcp.py' \
  <<< "$claude_opensandbox_web_mcp" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=EXA_API_KEY=fake-exa-key' \
  <<< "$claude_opensandbox_web_mcp" >/dev/null
grep -F -- "\"source\": \"$tmp/deps/exa_web_mcp.py\"" \
  <<< "$claude_opensandbox_web_mcp" >/dev/null
grep -F -- '"target": "/opt/agent-fleet/exa_web_mcp.py"' \
  <<< "$claude_opensandbox_web_mcp" >/dev/null

claude_opensandbox_traced="$(
  RUN_DRY_OPIK_URL='http://opik.example:5173' \
    run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py" \
      '{}' auto opensandbox 0 claude-code 0
)"
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_ENABLE_HOOK=true' \
  <<< "$claude_opensandbox_traced" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=TRACE_TO_OPIK=true' \
  <<< "$claude_opensandbox_traced" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=OPIK_URL_OVERRIDE=http://opik.example:5173/api' \
  <<< "$claude_opensandbox_traced" >/dev/null
grep -F -- "\"source\": \"$tmp/deps/claude_realtime_trace.py\"" \
  <<< "$claude_opensandbox_traced" >/dev/null
grep -F -- '"target": "/opt/tb-opik/claude_realtime_trace.py"' \
  <<< "$claude_opensandbox_traced" >/dev/null
grep -F -- '"read_only": true' <<< "$claude_opensandbox_traced" >/dev/null

claude_opensandbox_traceoff="$(
  RUN_DRY_CC_OPIK_ENABLE_HOOK=1 \
    run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py" \
      '{}' auto opensandbox 0 claude-code 0
)"
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_ENABLE_HOOK=false' \
  <<< "$claude_opensandbox_traceoff" >/dev/null
if grep -F -- "\"source\": \"$tmp/deps/claude_realtime_trace.py\"" \
  <<< "$claude_opensandbox_traceoff" >/dev/null; then
  echo 'trace-off OpenSandbox command unexpectedly mounts the Claude hook' >&2
  exit 1
fi
if grep -F -- 'FAKE_HARBOR_ARG=OPIK_URL_OVERRIDE=' \
  <<< "$claude_opensandbox_traceoff" >/dev/null; then
  echo 'trace-off OpenSandbox command unexpectedly exposes the Opik endpoint' >&2
  exit 1
fi
if grep -F -- 'FAKE_HARBOR_ARG=TRACE_TO_OPIK=true' \
  <<< "$claude_opensandbox_traceoff" >/dev/null; then
  echo 'trace-off OpenSandbox command unexpectedly enables task tracing' >&2
  exit 1
fi

claude_opensandbox_missing_hook="$(
  RUN_DRY_OPIK_URL='http://opik.example:5173' \
  RUN_DRY_CC_HOOK_SOURCE="$tmp/deps/missing-claude-hook.py" \
    run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py" \
      '{}' auto opensandbox 0 claude-code 0
)"
grep -F -- '[WARN] CC hook source not found, disable realtime hook:' \
  <<< "$claude_opensandbox_missing_hook" >/dev/null
grep -F -- 'set HARBOR_CC_HOOK_SOURCE to an existing file' \
  <<< "$claude_opensandbox_missing_hook" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_ENABLE_HOOK=false' \
  <<< "$claude_opensandbox_missing_hook" >/dev/null
if grep -F -- 'FAKE_HARBOR_ARG=TRACE_TO_OPIK=true' \
  <<< "$claude_opensandbox_missing_hook" >/dev/null; then
  echo 'missing-hook OpenSandbox command unexpectedly enables Claude hook tracing' >&2
  exit 1
fi
if grep -F -- "$tmp/deps/missing-claude-hook.py" \
  <<< "$claude_opensandbox_missing_hook" | grep -F -- '"source"' >/dev/null; then
  echo 'missing-hook OpenSandbox command unexpectedly mounts the missing hook' >&2
  exit 1
fi

claude_e2b_traced="$(
  RUN_DRY_OPIK_URL='http://opik.example:5173' \
    run_dry 'test-project/manual:immutable' "$tmp/does-not-exist.py" \
      '{}' auto e2b 0 claude-code 0
)"
grep -F -- 'FAKE_HARBOR_ARG=CC_OPIK_ENABLE_HOOK=false' \
  <<< "$claude_e2b_traced" >/dev/null
if grep -F -- 'FAKE_HARBOR_ARG=TRACE_TO_OPIK=true' \
  <<< "$claude_e2b_traced" >/dev/null; then
  echo 'traced E2B command unexpectedly enables the bind-mounted Claude hook' >&2
  exit 1
fi

opencode_opensandbox="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' auto \
  opensandbox 0 opencode 0)"
grep -F -- 'FAKE_HARBOR_ARG=--mounts-json' <<< "$opencode_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=OPENCODE_TGZ_PATH=' \
  <<< "$opencode_opensandbox" >/dev/null
grep -F -- 'FAKE_HARBOR_ARG=HARBOR_VERIFIER_UV_BIN_DIR=/opt/tb-uv-backup/bin' \
  <<< "$opencode_opensandbox" >/dev/null
if grep -F -- 'FAKE_HARBOR_ARG=XDG_CONFIG_HOME=' \
  <<< "$opencode_opensandbox" >/dev/null; then
  echo 'OpenSandbox OpenCode unexpectedly overrides the agent XDG home' >&2
  exit 1
fi

mkdir -p "$tmp/pi-extensions"
printf 'export default function () {}\n' > "$tmp/pi-extensions/smoke.ts"
pi_opensandbox="$(run_dry \
  'test-project/manual:immutable' "$tmp/does-not-exist.py" '{}' auto \
  opensandbox 0 pi 0 "$tmp/pi-extensions")"
grep -F -- 'FAKE_HARBOR_ARG=PI_EXTENSION_DIR=/opt/tb-pi/extensions' \
  <<< "$pi_opensandbox" >/dev/null
grep -F -- "\"source\": \"$tmp/pi-extensions\"" \
  <<< "$pi_opensandbox" >/dev/null
grep -F -- '"target": "/opt/tb-pi/extensions"' <<< "$pi_opensandbox" >/dev/null
grep -F -- '"read_only": true' <<< "$pi_opensandbox" >/dev/null
