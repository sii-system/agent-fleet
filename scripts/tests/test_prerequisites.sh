#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREREQUISITES="$REPO_ROOT/scripts/prerequisites.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

TEST_HOME="$TEST_ROOT/home"
XDG_DATA_HOME="$TEST_HOME/data"
MANAGED_BIN="$XDG_DATA_HOME/agent-fleet/bin"
SHARED_BIN="$TEST_HOME/.local/bin"
CACHE_DIR="$TEST_HOME/cache/agent-fleet"
RUNTIME_DIR="$TEST_HOME/runtime"
mkdir -p "$TEST_HOME" "$SHARED_BIN"

cat >"$SHARED_BIN/zellij" <<'EOF'
#!/usr/bin/env bash
echo 'zellij 0.45.0'
EOF
cat >"$SHARED_BIN/uv" <<'EOF'
#!/usr/bin/env bash
echo 'uv 0.12.0'
EOF
cat >"$SHARED_BIN/uvx" <<'EOF'
#!/usr/bin/env bash
echo 'uvx 0.12.0'
EOF
cat >"$SHARED_BIN/pi" <<'EOF'
#!/usr/bin/env bash
echo '9.9.9'
EOF
chmod +x "$SHARED_BIN/zellij" "$SHARED_BIN/uv" "$SHARED_BIN/uvx" "$SHARED_BIN/pi"

export HOME="$TEST_HOME"
export XDG_DATA_HOME
export PATH="$SHARED_BIN:$PATH"
unset AGENT_FLEET_BIN_DIR
export AGENT_FLEET_CACHE_DIR="$CACHE_DIR"
export AGENT_FLEET_RUNTIME_DIR="$RUNTIME_DIR"
export AGENT_FLEET_PREREQUISITES_FORCE_MANAGED=1
# shellcheck source=../prerequisites.sh
source "$PREREQUISITES"
[[ "$AGENT_FLEET_BIN_DIR" == "$MANAGED_BIN" ]]
agent_fleet_prerequisite_init_path
agent_fleet_prerequisite_init_runtime
[[ "$XDG_RUNTIME_DIR" == "$RUNTIME_DIR" ]]
[[ "$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$RUNTIME_DIR")" == "700" ]]

zellij_asset="$(agent_fleet_platform_asset zellij)"
uv_asset="$(agent_fleet_platform_asset uv)"
zellij_archive="$AGENT_FLEET_CACHE_DIR/downloads/zellij/$ZELLIJ_VERSION/$zellij_asset"
uv_archive="$AGENT_FLEET_CACHE_DIR/downloads/uv/$UV_VERSION/$uv_asset"
mkdir -p "$(dirname "$zellij_archive")" "$(dirname "$uv_archive")"

python3 - "$zellij_archive" "$uv_archive" <<'PY'
import io
import pathlib
import tarfile
import sys

def add_script(bundle, name, body):
    payload = body.encode()
    member = tarfile.TarInfo(name)
    member.mode = 0o755
    member.size = len(payload)
    bundle.addfile(member, io.BytesIO(payload))

zellij_archive = pathlib.Path(sys.argv[1])
uv_archive = pathlib.Path(sys.argv[2])
with tarfile.open(zellij_archive, "w:gz") as bundle:
    add_script(bundle, "zellij", "#!/usr/bin/env bash\necho 'zellij 0.44.3'\n")
with tarfile.open(uv_archive, "w:gz") as bundle:
    add_script(bundle, "uv-test/uv", "#!/usr/bin/env bash\necho 'uv 0.11.28'\n")
    add_script(bundle, "uv-test/uvx", "#!/usr/bin/env bash\necho 'uvx 0.11.28'\n")
PY

python3 - "$zellij_archive.sha256" \
  "$uv_archive" "$uv_archive.sha256" <<'PY'
import hashlib
import pathlib
import sys

zellij_payload = b"#!/usr/bin/env bash\necho 'zellij 0.44.3'\n"
pathlib.Path(sys.argv[1]).write_text(
    f"{hashlib.sha256(zellij_payload).hexdigest()}  target/release/zellij\n"
)
uv_source = pathlib.Path(sys.argv[2])
uv_digest = hashlib.sha256(uv_source.read_bytes()).hexdigest()
pathlib.Path(sys.argv[3]).write_text(f"{uv_digest}  {uv_source.name}\n")
PY

agent_fleet_install_zellij
agent_fleet_install_uv

[[ "$("$MANAGED_BIN/zellij" --version)" == "zellij 0.44.3" ]]
[[ "$("$MANAGED_BIN/uv" --version)" == "uv 0.11.28" ]]
[[ "$("$MANAGED_BIN/uvx" --version)" == "uvx 0.11.28" ]]
[[ "$(command -v zellij)" == "$MANAGED_BIN/zellij" ]]
[[ "$(command -v uv)" == "$MANAGED_BIN/uv" ]]
[[ "$UV_TOOL_BIN_DIR" == "$MANAGED_BIN" ]]
[[ "$UV_CACHE_DIR" == "$CACHE_DIR/uv/cache" ]]
[[ "$("$SHARED_BIN/zellij" --version)" == "zellij 0.45.0" ]]
[[ "$("$SHARED_BIN/uv" --version)" == "uv 0.12.0" ]]
[[ "$("$SHARED_BIN/uvx" --version)" == "uvx 0.12.0" ]]

NODE_BIN="$TEST_ROOT/node-bin"
NPM_BIN="$TEST_ROOT/npm-bin"
mkdir -p "$NODE_BIN" "$NPM_BIN"
cat >"$NODE_BIN/node" <<'EOF'
#!/usr/bin/env bash
echo 'v24.0.0'
EOF
cat >"$NPM_BIN/pi" <<'EOF'
#!/usr/bin/env bash
echo '0.81.1'
EOF
chmod +x "$NODE_BIN/node" "$NPM_BIN/pi"

AGENT_FLEET_NODE_BIN_DIR="$NODE_BIN"
AGENT_FLEET_NPM_BIN_DIR="$NPM_BIN"
PATH="$SHARED_BIN:$NPM_BIN:$PATH"
agent_fleet_prerequisite_init_path
path_after_init="$PATH"
agent_fleet_prerequisite_init_path
[[ "$PATH" == "$path_after_init" ]]
[[ "$(command -v pi)" == "$NPM_BIN/pi" ]]
agent_fleet_save_prerequisite_paths >/dev/null
if grep -q '^export AGENT_FLEET_RUNTIME_DIR=' "$AGENT_FLEET_PATHS_FILE"; then
  echo "UID-derived runtime path was persisted" >&2
  exit 1
fi

persisted_paths="$(
  HOME="$TEST_HOME" \
  XDG_DATA_HOME="$XDG_DATA_HOME" \
  PATH=/usr/bin:/bin \
  bash -c '
    unset AGENT_FLEET_BIN_DIR AGENT_FLEET_CACHE_DIR
    unset AGENT_FLEET_NODE_BIN_DIR AGENT_FLEET_NPM_BIN_DIR
    unset AGENT_FLEET_RUNTIME_DIR
    unset AGENT_FLEET_PATHS_FILE
    source "$1"
    agent_fleet_prerequisite_init_path
    initialized_path="$PATH"
    agent_fleet_prerequisite_init_path
    [[ "$PATH" == "$initialized_path" ]]
    command -v zellij
    command -v uv
    command -v node
    command -v pi
  ' bash "$PREREQUISITES"
)"
[[ "$persisted_paths" == \
"$MANAGED_BIN/zellij
$MANAGED_BIN/uv
$NODE_BIN/node
$NPM_BIN/pi" ]]

direct_help="$(
  env -u AGENT_FLEET_BIN_DIR -u AGENT_FLEET_CACHE_DIR \
    -u AGENT_FLEET_NODE_BIN_DIR -u AGENT_FLEET_NPM_BIN_DIR \
    -u AGENT_FLEET_RUNTIME_DIR -u AGENT_FLEET_PATHS_FILE \
    HOME="$TEST_HOME" \
    PATH=/usr/bin:/bin \
    "$REPO_ROOT/scripts/run_fleet.sh" --help
)"
grep -Fq 'Usage:' <<<"$direct_help"

BLOCKER="$TEST_ROOT/not-a-directory"
printf 'block\n' >"$BLOCKER"
saved_paths_file="$AGENT_FLEET_PATHS_FILE"
AGENT_FLEET_PATHS_FILE="$BLOCKER/paths.env"
if agent_fleet_save_prerequisite_paths >/dev/null 2>&1; then
  echo "saving prerequisite paths unexpectedly succeeded through a file" >&2
  exit 1
fi
AGENT_FLEET_PATHS_FILE="$saved_paths_file"

saved_runtime_dir="$AGENT_FLEET_RUNTIME_DIR"
saved_xdg_runtime_dir="${XDG_RUNTIME_DIR:-}"
unset XDG_RUNTIME_DIR
AGENT_FLEET_RUNTIME_DIR="$BLOCKER/runtime"
if agent_fleet_prerequisite_init_runtime >/dev/null 2>&1; then
  echo "runtime initialization unexpectedly succeeded through a file" >&2
  exit 1
fi
AGENT_FLEET_RUNTIME_DIR="$saved_runtime_dir"
XDG_RUNTIME_DIR="$saved_xdg_runtime_dir"

SYMLINK_TARGET="$TEST_ROOT/runtime-target"
SYMLINK_RUNTIME="$TEST_ROOT/runtime-link"
mkdir -m 0755 "$SYMLINK_TARGET"
ln -s "$SYMLINK_TARGET" "$SYMLINK_RUNTIME"
unset XDG_RUNTIME_DIR
AGENT_FLEET_RUNTIME_DIR="$SYMLINK_RUNTIME"
if agent_fleet_prerequisite_init_runtime >/dev/null 2>&1; then
  echo "runtime initialization unexpectedly accepted a symbolic link" >&2
  exit 1
fi
[[ "$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$SYMLINK_TARGET")" == "755" ]]

DANGLING_RUNTIME="$TEST_ROOT/dangling-runtime"
ln -s "$TEST_ROOT/missing-runtime-target" "$DANGLING_RUNTIME"
AGENT_FLEET_RUNTIME_DIR="$DANGLING_RUNTIME"
if agent_fleet_prerequisite_init_runtime >/dev/null 2>&1; then
  echo "runtime initialization unexpectedly accepted a dangling symbolic link" >&2
  exit 1
fi

AGENT_FLEET_RUNTIME_DIR="$saved_runtime_dir"
XDG_RUNTIME_DIR="$saved_xdg_runtime_dir"

# Docker stays required by default, for the docker backend, and for
# opensandbox (task images build on the runner's local daemon); qz/e2b build
# nothing locally and are exempt, with AGENT_FLEET_REQUIRE_DOCKER as the
# explicit override in both directions.
docker_required() {
  env -u RL_ENVIRONMENT_TYPE -u HARBOR_ENVIRONMENT_TYPE -u AGENT_FLEET_REQUIRE_DOCKER \
    "$@" bash -c 'source "$1"; agent_fleet_docker_required' _ "$PREREQUISITES"
}
docker_required
docker_required RL_ENVIRONMENT_TYPE=docker
docker_required RL_ENVIRONMENT_TYPE=qz AGENT_FLEET_REQUIRE_DOCKER=1
if docker_required RL_ENVIRONMENT_TYPE=qz; then
  echo "docker unexpectedly required for the qz backend" >&2
  exit 1
fi
if docker_required HARBOR_ENVIRONMENT_TYPE=e2b; then
  echo "docker unexpectedly required for the e2b backend" >&2
  exit 1
fi
docker_required RL_ENVIRONMENT_TYPE=opensandbox
# The per-run Harbor backend overrides the RL fallback, matching env.sh.
docker_required RL_ENVIRONMENT_TYPE=qz HARBOR_ENVIRONMENT_TYPE=opensandbox
if docker_required RL_ENVIRONMENT_TYPE=docker HARBOR_ENVIRONMENT_TYPE=qz; then
  echo "docker unexpectedly required when Harbor overrides RL with qz" >&2
  exit 1
fi
if docker_required AGENT_FLEET_REQUIRE_DOCKER=0; then
  echo "docker unexpectedly required with AGENT_FLEET_REQUIRE_DOCKER=0" >&2
  exit 1
fi

# An unused or incomplete docker CLI must not make a remote backend depend on
# the Compose plugin.
if ! env -u RL_ENVIRONMENT_TYPE -u HARBOR_ENVIRONMENT_TYPE \
  -u AGENT_FLEET_REQUIRE_DOCKER RL_ENVIRONMENT_TYPE=qz \
  bash -c '
    source "$1"
    agent_fleet_check_commands() { return 0; }
    agent_fleet_python_version_ok() { return 0; }
    docker() { return 1; }
    agent_fleet_check_core
  ' _ "$PREREQUISITES"; then
  echo "qz prerequisites unexpectedly required a working Docker Compose" >&2
  exit 1
fi

echo "prerequisite tests passed"
