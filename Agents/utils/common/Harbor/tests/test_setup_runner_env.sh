#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

make_runner() {
  local runner_dir="$1"
  local harbor_version="$2"
  local opik_version="$3"
  local python_version="${4:-3.12.13}"
  mkdir -p "$runner_dir/bin"
  cat > "$runner_dir/bin/python" <<SH
#!/usr/bin/env bash
if [[ "\${3:-}" == "harbor" ]]; then
  printf '%s\n' '$harbor_version'
elif [[ "\${3:-}" == "e2b" ]]; then
  printf '%s\n' '2.32.1'
elif [[ "\${3:-}" == "opik" ]]; then
  printf '%s\n' '$opik_version'
elif [[ "\${3:-}" == "yicloud-sdk-python" ]]; then
  printf '%s\n' '0.3.1'
elif [[ "\${3:-}" == "pip" ]]; then
  printf '%s\n' '26.2'
elif [[ "\${3:-}" == "PyYAML" ]]; then
  printf '%s\n' '6.0.3'
elif [[ "\${3:-}" == "s3cmd" ]]; then
  printf '%s\n' '2.4.0'
elif [[ "\${3:-}" == "e2b" ]]; then
  printf '%s\n' '2.32.1'
elif [[ "\${3:-}" == "dockerfile-parse" ]]; then
  printf '%s\n' '2.0.1'
elif [[ "\$#" == 2 ]]; then
  printf '%s\n' '$python_version'
else
  exit 1
fi
SH
  cat > "$runner_dir/bin/opik" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$runner_dir/bin/harbor" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$runner_dir/bin/python" "$runner_dir/bin/opik" "$runner_dir/bin/harbor"
}

IMAGE_RUNNER="$TMP_DIR/image-runner"
HOST_RUNNER="$TMP_DIR/host-runner"
make_runner "$IMAGE_RUNNER" 0.18.0 2.1.32
HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
HARBOR_RUNNER_UV_BIN="$TMP_DIR/missing-uv" \
  "$HARBOR_DIR/setup_runner_env.sh"
[[ ! -e "$HOST_RUNNER" ]]

make_runner "$IMAGE_RUNNER" 0.20.0 2.1.32
if HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
  HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
  HARBOR_RUNNER_UV_BIN="$TMP_DIR/missing-uv" \
  "$HARBOR_DIR/setup_runner_env.sh" >"$TMP_DIR/image-mismatch.log" 2>&1; then
  echo "setup accepted a mismatched image runner or fell back to the host" >&2
  exit 1
fi
grep -q '0.20.0' "$TMP_DIR/image-mismatch.log"

rm -rf "$IMAGE_RUNNER" "$HOST_RUNNER"
FAKE_UV="$TMP_DIR/uv"
FAKE_RUNNER="$TMP_DIR/fake-runner"
make_runner "$FAKE_RUNNER" 0.18.0 2.1.32
UV_LOG="$TMP_DIR/uv.log"
cat > "$FAKE_UV" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$UV_LOG"
if [[ "${1:-}" == "venv" ]]; then
  if [[ "${FAIL_PYTHON_MIRROR:-0}" == "1" && -n "${UV_PYTHON_INSTALL_MIRROR:-}" ]]; then
    exit 42
  fi
  runner_dir="${@: -1}"
  rm -rf "$runner_dir"
  cp -a "$FAKE_RUNNER" "$runner_dir"
fi
SH
chmod +x "$FAKE_UV"

UV_LOG="$UV_LOG" \
FAKE_RUNNER="$FAKE_RUNNER" \
HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
HARBOR_RUNNER_UV_BIN="$FAKE_UV" \
  "$HARBOR_DIR/setup_runner_env.sh"

grep -q "venv --clear --python 3.12.13 $HOST_RUNNER" "$UV_LOG"
grep -q "pip install --only-binary :all: --python $HOST_RUNNER/bin/python --requirement $HARBOR_DIR/runner-requirements.txt" "$UV_LOG"
grep -q "pip check --python $HOST_RUNNER/bin/python" "$UV_LOG"

rm -rf "$HOST_RUNNER"
: > "$UV_LOG"
MIRROR_FALLBACK_LOG="$TMP_DIR/mirror-fallback.log"
UV_LOG="$UV_LOG" \
FAKE_RUNNER="$FAKE_RUNNER" \
FAIL_PYTHON_MIRROR=1 \
HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
HARBOR_RUNNER_UV_BIN="$FAKE_UV" \
  "$HARBOR_DIR/setup_runner_env.sh" > "$MIRROR_FALLBACK_LOG"

[[ "$(grep -c "venv --clear --python 3.12.13 $HOST_RUNNER" "$UV_LOG")" -eq 2 ]]
grep -q 'Python mirror failed; falling back to the upstream release' "$MIRROR_FALLBACK_LOG"
grep -q "pip install --only-binary :all: --python $HOST_RUNNER/bin/python --requirement $HARBOR_DIR/runner-requirements.txt" "$UV_LOG"

CUSTOM_RUNNER="$TMP_DIR/custom-runner"
CUSTOM_HOST_RUNNER="$TMP_DIR/custom-host-runner"
CUSTOM_IMAGE_RUNNER="$TMP_DIR/no-custom-image-runner"
make_runner "$CUSTOM_RUNNER" 0.18.0 2.1.32 3.12.3

if HARBOR_RUNNER_PYTHON_VERSION=3.12.13 \
  HARBOR_RUNNER_PYTHON=3.12.3 \
  HARBOR_RUNNER_IMAGE_DIR="$CUSTOM_IMAGE_RUNNER" \
  HARBOR_RUNNER_HOST_DIR="$CUSTOM_HOST_RUNNER" \
  HARBOR_RUNNER_UV_BIN="$FAKE_UV" \
  "$HARBOR_DIR/setup_runner_env.sh" > "$TMP_DIR/setup-version-conflict.log" 2>&1; then
  echo "setup accepted conflicting runner Python variables" >&2
  exit 1
fi
grep -q 'HARBOR_RUNNER_PYTHON_VERSION and legacy HARBOR_RUNNER_PYTHON disagree' \
  "$TMP_DIR/setup-version-conflict.log"

: > "$UV_LOG"
UV_LOG="$UV_LOG" \
FAKE_RUNNER="$CUSTOM_RUNNER" \
HARBOR_RUNNER_PYTHON_VERSION=3.12.3 \
HARBOR_RUNNER_IMAGE_DIR="$CUSTOM_IMAGE_RUNNER" \
HARBOR_RUNNER_HOST_DIR="$CUSTOM_HOST_RUNNER" \
HARBOR_RUNNER_UV_BIN="$FAKE_UV" \
  "$HARBOR_DIR/setup_runner_env.sh"
grep -q "venv --clear --python 3.12.3 $CUSTOM_HOST_RUNNER" "$UV_LOG"
[[ "$(cat "$CUSTOM_HOST_RUNNER/.agent-fleet-python-version")" == "3.12.3" ]]

rm -rf "$CUSTOM_HOST_RUNNER"
: > "$UV_LOG"
UV_LOG="$UV_LOG" \
FAKE_RUNNER="$CUSTOM_RUNNER" \
HARBOR_RUNNER_PYTHON=3.12.3 \
HARBOR_RUNNER_IMAGE_DIR="$CUSTOM_IMAGE_RUNNER" \
HARBOR_RUNNER_HOST_DIR="$CUSTOM_HOST_RUNNER" \
HARBOR_RUNNER_UV_BIN="$FAKE_UV" \
  "$HARBOR_DIR/setup_runner_env.sh"
grep -q "venv --clear --python 3.12.3 $CUSTOM_HOST_RUNNER" "$UV_LOG"

runtime_python_version="$(
  env -i \
    HOME="$TMP_DIR/home" \
    PATH="$PATH" \
    OUTPUT_ROOT="$TMP_DIR/output" \
    RUN_ID=runner-version-test \
    HARBOR_RUNNER_IMAGE_DIR="$CUSTOM_IMAGE_RUNNER" \
    HARBOR_RUNNER_HOST_DIR="$CUSTOM_HOST_RUNNER" \
    bash -c '. "$1/env.sh"; python3 "$1/harbor_prepare_runner_cli.py" --validate; printf "%s" "$HARBOR_RUNNER_PYTHON_VERSION"' \
      bash "$HARBOR_DIR"
)"
[[ "$runtime_python_version" == "3.12.3" ]]

if env -i \
  HOME="$TMP_DIR/home" \
  PATH="$PATH" \
  OUTPUT_ROOT="$TMP_DIR/output" \
  RUN_ID=runner-version-conflict \
  HARBOR_RUNNER_PYTHON_VERSION=3.12.13 \
  HARBOR_RUNNER_PYTHON=3.12.3 \
  HARBOR_RUNNER_IMAGE_DIR="$CUSTOM_IMAGE_RUNNER" \
  HARBOR_RUNNER_HOST_DIR="$CUSTOM_HOST_RUNNER" \
  bash -c '. "$1/env.sh"' bash "$HARBOR_DIR" \
  > "$TMP_DIR/runtime-version-conflict.log" 2>&1; then
  echo "runtime accepted conflicting runner Python variables" >&2
  exit 1
fi
grep -q 'HARBOR_RUNNER_PYTHON_VERSION and legacy HARBOR_RUNNER_PYTHON disagree' \
  "$TMP_DIR/runtime-version-conflict.log"

selected_host="$(
  env -i \
    HOME="$TMP_DIR/home" \
    PATH="$PATH" \
    OUTPUT_ROOT="$TMP_DIR/output" \
    RUN_ID=runner-path-test \
    HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
    HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
    bash -c '. "$1/env.sh"; printf "%s|%s|%s" "$HARBOR_OPIK_BIN" "$HARBOR_CLI_BIN" "$HARBOR_OPIK_PYTHON"' \
      bash "$HARBOR_DIR"
)"
[[ "$selected_host" == "$HOST_RUNNER/bin/opik|$HOST_RUNNER/bin/harbor|$HOST_RUNNER/bin/python" ]]

make_runner "$IMAGE_RUNNER" 0.18.0 2.1.32
selected_image="$(
  env -i \
    HOME="$TMP_DIR/home" \
    PATH="$PATH" \
    OUTPUT_ROOT="$TMP_DIR/output" \
    RUN_ID=runner-path-test \
    HARBOR_RUNNER_IMAGE_DIR="$IMAGE_RUNNER" \
    HARBOR_RUNNER_HOST_DIR="$HOST_RUNNER" \
    bash -c '. "$1/env.sh"; printf "%s|%s|%s" "$HARBOR_OPIK_BIN" "$HARBOR_CLI_BIN" "$HARBOR_OPIK_PYTHON"' \
      bash "$HARBOR_DIR"
)"
[[ "$selected_image" == "$IMAGE_RUNNER/bin/opik|$IMAGE_RUNNER/bin/harbor|$IMAGE_RUNNER/bin/python" ]]
