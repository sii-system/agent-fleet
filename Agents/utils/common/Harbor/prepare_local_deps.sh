#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEEL_DIR="${WHEEL_DIR:-${SCRIPT_DIR}/python-wheels}"
PYTHON_BIN="${PYTHON_BIN:-${HARBOR_OPIK_PYTHON:-python3.12}}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-latest}"
OPENCODE_VERSION="${OPENCODE_VERSION:-latest}"
PREPARE_OPENCODE_CACHE="${PREPARE_OPENCODE_CACHE:-0}"
PI_VERSION="${PI_VERSION:-0.81.1}"
PREPARE_PI_CACHE="${PREPARE_PI_CACHE:-0}"
NPM_REGISTRY_URL="${NPM_REGISTRY_URL:-${NPM_CONFIG_REGISTRY:-https://registry.npmjs.org}}"
CLAUDE_CODE_NPM_SPEC="@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
CLAUDE_CODE_TGZ_BASENAME="${CLAUDE_CODE_TGZ_BASENAME:-claude-code-${CLAUDE_CODE_VERSION}.tgz}"
OPENCODE_TGZ_BASENAME="${OPENCODE_TGZ_BASENAME:-opencode-ai-${OPENCODE_VERSION}.tgz}"
OPENCODE_LINUX_X64_TGZ_BASENAME="${OPENCODE_LINUX_X64_TGZ_BASENAME:-opencode-linux-x64-${OPENCODE_VERSION}.tgz}"
PI_NPM_SPEC="@earendil-works/pi-coding-agent@${PI_VERSION}"
PI_TGZ_BASENAME="${PI_TGZ_BASENAME:-pi-coding-agent-${PI_VERSION}.tgz}"
PI_NODE_RUNTIME_BASENAME="${PI_NODE_RUNTIME_BASENAME:-pi-node-runtime.tar.gz}"
PI_RUNTIME_BASENAME="${PI_RUNTIME_BASENAME:-pi-runtime-${PI_VERSION}.tar.gz}"
PY312_RUNTIME_TARBALL="${PY312_RUNTIME_TARBALL:-${WHEEL_DIR}/python3.12-runtime.tar.gz}"
NODE_RUNTIME_TARBALL="${NODE_RUNTIME_TARBALL:-${WHEEL_DIR}/node-runtime.tar.xz}"
PI_NODE_RUNTIME_TARBALL="${PI_NODE_RUNTIME_TARBALL:-${WHEEL_DIR}/${PI_NODE_RUNTIME_BASENAME}}"
PI_RUNTIME_TARBALL="${PI_RUNTIME_TARBALL:-${WHEEL_DIR}/${PI_RUNTIME_BASENAME}}"
CLAUDE_NPM_CACHE_DIR="${CLAUDE_NPM_CACHE_DIR:-${WHEEL_DIR}/npm-cache}"
CACHE_SCHEMA="${CACHE_SCHEMA:-3}"

mkdir -p "$WHEEL_DIR"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python binary not found: $PYTHON_BIN" >&2
  exit 1
fi

echo "[prepare] target dir: $WHEEL_DIR"

have_wheel() {
  local pattern="$1"
  ls "$WHEEL_DIR"/$pattern >/dev/null 2>&1
}

count_wheels() {
  local pattern="$1"
  find "$WHEEL_DIR" -maxdepth 1 -name "$pattern" -type f | wc -l | tr -d ' '
}

tarball_ready() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  "$PYTHON_BIN" - "$path" <<'PY' >/dev/null 2>&1
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    archive.getmembers()
PY
}

ensure_clean_opik_cache() {
  local opik_count
  opik_count="$(count_wheels 'opik-*.whl')"
  if ! grep -qx "cache_schema=${CACHE_SCHEMA}" "$WHEEL_DIR/manifest.txt" 2>/dev/null \
    || [[ "$opik_count" != "1" ]]; then
    # This is a shared cache used by both claude-code and opencode. Keep one
    # Opik wheel only; multiple versions make offline task installs unstable.
    rm -f "$WHEEL_DIR"/opik-*.whl
  fi
}

download_pkg() {
  local pkg="$1"
  local pattern="$2"
  if have_wheel "$pattern"; then
    echo "[prepare] skip $pkg (cached)"
    return 0
  fi
  # Download complete dependency graph once so task runtime can stay offline.
  "$PYTHON_BIN" -m pip download --disable-pip-version-check \
    --dest "$WHEEL_DIR" \
    "$pkg"
}

ensure_clean_opik_cache
download_pkg "opik" "opik-*.whl"
download_pkg "uuid6" "uuid6-*.whl"
download_pkg "socksio" "socksio-*.whl"
download_pkg "pip" "pip-*.whl"
download_pkg "setuptools" "setuptools-*.whl"
download_pkg "wheel" "wheel-*.whl"

download_py313_hook_wheels() {
  if have_wheel "rapidfuzz-*-cp313-*.whl" \
    && have_wheel "watchfiles-*-cp313-*.whl" \
    && have_wheel "pydantic_core-*-cp313-*.whl"; then
    echo "[prepare] skip Python 3.13 hook wheels (cached)"
    return 0
  fi

  # Some Harbor task images use Python 3.13. Cache the binary wheels needed by
  # the Opik hook so those containers do not have to go online at install time.
  "$PYTHON_BIN" -m pip download --disable-pip-version-check \
    --dest "$WHEEL_DIR" \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --python-version 3.13 \
    --implementation cp \
    --abi cp313 \
    opik uuid6 socksio
}

download_py313_hook_wheels

if [[ "$(count_wheels 'opik-*.whl')" != "1" ]]; then
  echo "expected exactly one opik wheel after prepare" >&2
  find "$WHEEL_DIR" -maxdepth 1 -name 'opik-*.whl' -type f >&2
  exit 1
fi

build_py312_runtime_tarball() {
  if [[ -f "$PY312_RUNTIME_TARBALL" ]]; then
    echo "[prepare] skip python3.12 runtime tarball (cached)"
    return 0
  fi

  local tmp_dir runtime_root py_real stdlib libpython
  tmp_dir="$(mktemp -d)"
  runtime_root="${tmp_dir}/python3.12-runtime"
  mkdir -p "${runtime_root}/bin" "${runtime_root}/lib"

  py_real="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
  stdlib="$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
  libpython="$("$PYTHON_BIN" -c 'import sysconfig, pathlib; libdir = pathlib.Path(sysconfig.get_config_var("LIBDIR") or ""); ver = (sysconfig.get_config_var("VERSION") or "3.12"); cands = sorted(libdir.glob(f"libpython{ver}*.so*")); print(cands[0] if cands else "")')"

  cp -L "$py_real" "${runtime_root}/bin/python3.12.real"
  cp -a "$stdlib" "${runtime_root}/lib/python3.12"
  if [[ -n "${libpython:-}" && -f "${libpython}" ]]; then
    cp -a "${libpython}" "${runtime_root}/lib/"
  fi

  mkdir -p "${runtime_root}/lib/system"
  ldd "${runtime_root}/bin/python3.12.real" \
    | awk '{for(i=1;i<=NF;i++) if ($i ~ /^\//) print $i}' \
    | while read -r so; do
      [[ -f "$so" ]] || continue
      cp -a "$so" "${runtime_root}/lib/system/" || true
    done

  cat > "${runtime_root}/bin/python3.12" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd "${SELF_DIR}/.." && pwd)"
export PYTHONHOME="${RUNTIME_ROOT}"
export LD_LIBRARY_PATH="${RUNTIME_ROOT}/lib/system:${RUNTIME_ROOT}/lib:${LD_LIBRARY_PATH:-}"
exec "${SELF_DIR}/python3.12.real" "$@"
EOS
  chmod +x "${runtime_root}/bin/python3.12"

  tar -C "${tmp_dir}" -czf "${PY312_RUNTIME_TARBALL}" "python3.12-runtime"
  echo "[prepare] built python3.12 runtime tarball: ${PY312_RUNTIME_TARBALL}"
  rm -rf "${tmp_dir}"
}

build_py312_runtime_tarball

prepare_node_runtime_tarball() {
  if tarball_ready "$NODE_RUNTIME_TARBALL"; then
    echo "[prepare] skip node runtime tarball (cached)"
    return 0
  fi
  rm -f "$NODE_RUNTIME_TARBALL"

  "$PYTHON_BIN" - <<PY2
import json
import os
import tempfile
import time
import tarfile
import urllib.request
from pathlib import Path

index = json.load(urllib.request.urlopen("https://nodejs.org/dist/index.json"))
version = next(item["version"] for item in index if str(item.get("version", "")).startswith("v22."))
url = f"https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz"
out = Path(r"$NODE_RUNTIME_TARBALL")
last_error = None
for attempt in range(1, 4):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="node-runtime-", suffix=".tar.xz", dir=str(out.parent))
        os.close(fd)
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp) as archive:
            archive.getmembers()
        os.replace(tmp, out)
        print(f"downloaded node runtime tarball: {url}")
        break
    except Exception as exc:
        last_error = exc
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        if attempt == 3:
            raise
        print(f"download node runtime attempt {attempt}/3 failed: {exc}; retrying", flush=True)
        time.sleep(2 * attempt)
PY2
}

prepare_node_runtime_tarball

prepare_pi_node_runtime_tarball() {
  if [[ "$PREPARE_PI_CACHE" != "1" ]]; then
    echo "[prepare] skip Pi portable node tar (PREPARE_PI_CACHE=0)"
    return 0
  fi
  if tarball_ready "$PI_NODE_RUNTIME_TARBALL"; then
    echo "[prepare] skip Pi portable node tar (cached)"
    return 0
  fi
  rm -f "$PI_NODE_RUNTIME_TARBALL"
  "$PYTHON_BIN" "$SCRIPT_DIR/env.py" portable-tar \
    "$NODE_RUNTIME_TARBALL" "$PI_NODE_RUNTIME_TARBALL"
  echo "[prepare] built Pi portable node tarball: $PI_NODE_RUNTIME_TARBALL"
}

prepare_pi_node_runtime_tarball

ensure_prepare_npm() {
  if command -v npm >/dev/null 2>&1; then
    return 0
  fi
  if ! tarball_ready "$NODE_RUNTIME_TARBALL"; then
    return 1
  fi
  local node_dir node_bin
  node_dir="$(mktemp -d /tmp/tb-prepare-node-XXXXXX)"
  "$PYTHON_BIN" - "$NODE_RUNTIME_TARBALL" "$node_dir" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2])
PY
  node_bin="$(find "$node_dir" -path '*/bin/npm' -print -quit 2>/dev/null || true)"
  if [[ -z "${node_bin:-}" ]]; then
    return 1
  fi
  # Keep the extracted runtime for the rest of this prepare process.
  export PATH="$(dirname "$node_bin"):$PATH"
}

# Cache get-pip bootstrap script for offline pip bootstrap inside task containers.
if [[ ! -f "$WHEEL_DIR/get-pip.py" ]]; then
  "$PYTHON_BIN" - <<PY
import urllib.request
urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", r"$WHEEL_DIR/get-pip.py")
print("downloaded get-pip.py")
PY
else
  echo "[prepare] skip get-pip.py (cached)"
fi

# Cache claude tarball locally. Prefer npm pack when available, otherwise fetch
# the tarball URL from the npm registry directly.
pack_npm_to_cache() {
  local npm_spec="$1"
  local target_basename="$2"
  local registry_meta_url="$3"
  local latest_glob="$4"

  if command -v npm >/dev/null 2>&1; then
    if [[ ! -f "$WHEEL_DIR/${target_basename}" ]]; then
      tmp_dir="$(mktemp -d)"
      trap 'rm -rf "$tmp_dir"' EXIT
      (
        cd "$tmp_dir"
        pkg="$(npm pack --registry "$NPM_REGISTRY_URL" "$npm_spec")"
        mv "$pkg" "$WHEEL_DIR/"
      )
    fi
    latest_tgz="$(ls -1t "$WHEEL_DIR"/$latest_glob 2>/dev/null | head -n 1 || true)"
    if [[ -n "${latest_tgz:-}" ]]; then
      cp -f "$latest_tgz" "$WHEEL_DIR/${target_basename}"
    fi
    return 0
  fi

  if [[ ! -f "$WHEEL_DIR/${target_basename}" ]]; then
    "$PYTHON_BIN" - <<PY2
import json
import urllib.request
meta = json.load(urllib.request.urlopen(r"$registry_meta_url"))
url = meta["dist"]["tarball"]
urllib.request.urlretrieve(url, r"$WHEEL_DIR/$target_basename")
print(f"downloaded npm tarball: {url}")
PY2
  else
    echo "[prepare] skip ${target_basename} (cached)"
  fi
}

download_npm_tgz_to_cache() {
  local target_basename="$1"
  local registry_meta_url="$2"

  if [[ -f "$WHEEL_DIR/${target_basename}" ]] && gzip -t "$WHEEL_DIR/${target_basename}" >/dev/null 2>&1; then
    echo "[prepare] skip ${target_basename} (cached)"
    return 0
  fi
  rm -f "$WHEEL_DIR/${target_basename}"

  "$PYTHON_BIN" - <<PY
import json
import os
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

meta_url = r"$registry_meta_url"
target = r"$WHEEL_DIR/$target_basename"
registry_origin = r"${NPM_REGISTRY_URL%/}"
last_error = None
for attempt in range(1, 4):
    tmp = None
    try:
        with urllib.request.urlopen(meta_url, timeout=60) as response:
            meta = json.load(response)
        original_url = meta["dist"]["tarball"]
        url = original_url
        # Nexus metadata may still point at registry.npmjs.org. Reuse the same
        # registry origin as metadata so large platform tarballs stay on mirror.
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc == "registry.npmjs.org" and "registry.npmjs.org" not in registry_origin:
            url = registry_origin.rstrip("/") + parsed.path
        # Some Nexus npm mirrors expose package metadata but return 404 for
        # large optional-platform tarballs. Try the mirror first, then the
        # original registry tarball before failing the cache preparation.
        urls = [url]
        if original_url not in urls:
            urls.append(original_url)
        fd, tmp = tempfile.mkstemp(prefix="npm-tgz-", suffix=".tgz", dir=os.path.dirname(target))
        last_download_error = None
        for candidate_url in urls:
            try:
                with os.fdopen(fd, "wb") as output, urllib.request.urlopen(candidate_url, timeout=120) as response:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                url = candidate_url
                break
            except Exception as exc:
                last_download_error = exc
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                fd, tmp = tempfile.mkstemp(prefix="npm-tgz-", suffix=".tgz", dir=os.path.dirname(target))
        else:
            raise last_download_error
        try:
            os.close(fd)
        except OSError:
            pass
        with tarfile.open(tmp, "r:gz") as archive:
            archive.getmembers()
        os.replace(tmp, target)
        print(f"downloaded npm tarball: {url}")
        break
    except Exception as exc:
        last_error = exc
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        if attempt == 3:
            raise
        print(f"download npm tarball attempt {attempt}/3 failed: {exc}; retrying", flush=True)
        time.sleep(2 * attempt)
PY
}

claude_meta_url="$(
  if [[ "$CLAUDE_CODE_VERSION" == "latest" ]]; then
    printf '%s\n' "${NPM_REGISTRY_URL%/}/@anthropic-ai/claude-code/latest"
  else
    printf '%s\n' "${NPM_REGISTRY_URL%/}/@anthropic-ai/claude-code/${CLAUDE_CODE_VERSION}"
  fi
)"
opencode_meta_url="$(
  if [[ "$OPENCODE_VERSION" == "latest" ]]; then
    printf '%s\n' "${NPM_REGISTRY_URL%/}/opencode-ai/latest"
  else
    printf '%s\n' "${NPM_REGISTRY_URL%/}/opencode-ai/${OPENCODE_VERSION}"
  fi
)"
pi_meta_url="$(
  printf '%s\n' "${NPM_REGISTRY_URL%/}/@earendil-works/pi-coding-agent/${PI_VERSION}"
)"
pack_npm_to_cache "$CLAUDE_CODE_NPM_SPEC" "$CLAUDE_CODE_TGZ_BASENAME" "$claude_meta_url" "anthropic-ai-claude-code-*.tgz"

prepare_claude_npm_cache() {
  if [[ -d "$CLAUDE_NPM_CACHE_DIR/_cacache" ]] \
    && grep -qx "claude_npm_cache_version=${CLAUDE_CODE_VERSION}" "$WHEEL_DIR/manifest.txt" 2>/dev/null; then
    echo "[prepare] skip Claude npm cache (cached)"
    return 0
  fi
  ensure_prepare_npm || true
  if ! command -v npm >/dev/null 2>&1; then
    echo "[prepare] npm not found; cannot prepare Claude npm cache" >&2
    return 1
  fi

  rm -rf "$CLAUDE_NPM_CACHE_DIR"
  mkdir -p "$CLAUDE_NPM_CACHE_DIR"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  (
    cd "$tmp_dir"
    # Populate npm's content-addressed cache for the selected Claude Code
    # version, including platform optional packages. Task containers then use
    # this cache with `npm --offline` instead of resolving from registry.
    npm install \
      --registry "$NPM_REGISTRY_URL" \
      --cache "$CLAUDE_NPM_CACHE_DIR" \
      --ignore-scripts \
      --no-audit \
      --fund=false \
      "$CLAUDE_CODE_NPM_SPEC"
  )
  rm -rf "$tmp_dir"
}

prepare_claude_npm_cache
printf '%s\n' "$CLAUDE_CODE_VERSION" > "$WHEEL_DIR/npm-cache-ready"

prepare_pi_runtime_tarball() {
  if [[ "$PREPARE_PI_CACHE" != "1" ]]; then
    echo "[prepare] skip Pi runtime tar (PREPARE_PI_CACHE=0)"
    return 0
  fi
  pack_npm_to_cache "$PI_NPM_SPEC" "$PI_TGZ_BASENAME" "$pi_meta_url" \
    "earendil-works-pi-coding-agent-*.tgz"
  if tarball_ready "$PI_RUNTIME_TARBALL" \
    && grep -qx "pi_runtime_version=${PI_VERSION}" "$WHEEL_DIR/manifest.txt" 2>/dev/null; then
    echo "[prepare] skip Pi runtime tar (cached)"
    return 0
  fi
  ensure_prepare_npm
  local tmp_dir runtime_prefix runtime_tar_tmp runtime_version
  tmp_dir="$(mktemp -d /tmp/tb-prepare-pi-XXXXXX)"
  runtime_prefix="$tmp_dir/pi-runtime"
  runtime_tar_tmp="$(mktemp "${WHEEL_DIR}/.${PI_RUNTIME_BASENAME}.XXXXXX")"
  npm install --global \
    --prefix "$runtime_prefix" \
    --registry "$NPM_REGISTRY_URL" \
    --cache "$CLAUDE_NPM_CACHE_DIR" \
    --ignore-scripts \
    --no-audit \
    --fund=false \
    "$WHEEL_DIR/$PI_TGZ_BASENAME"
  runtime_version="$("$runtime_prefix/bin/pi" --version)"
  if [[ "$runtime_version" != "$PI_VERSION" ]]; then
    echo "prepared Pi runtime version mismatch: expected $PI_VERSION, got $runtime_version" >&2
    rm -rf "$tmp_dir"
    rm -f "$runtime_tar_tmp"
    return 1
  fi
  tar -C "$runtime_prefix" -czf "$runtime_tar_tmp" .
  mv -f "$runtime_tar_tmp" "$PI_RUNTIME_TARBALL"
  rm -rf "$tmp_dir"
  echo "[prepare] built Pi runtime tarball: $PI_RUNTIME_TARBALL"
}

prepare_pi_runtime_tarball

if [[ "$PREPARE_OPENCODE_CACHE" == "1" ]]; then
  # OpenCode packages are plain npm tarballs. Download them from registry
  # metadata instead of npm pack so monitor-side cache preparation does not
  # depend on npm network behavior. Cache the common glibc x64 optional binary
  # too; npm still handles other platforms through the normal fallback path.
  download_npm_tgz_to_cache "$OPENCODE_TGZ_BASENAME" "$opencode_meta_url"
  opencode_linux_x64_meta_url="$(
    "$PYTHON_BIN" - <<PY
import json
import urllib.request
meta = json.load(urllib.request.urlopen(r"$opencode_meta_url"))
version = meta["version"]
if r"$OPENCODE_VERSION" == "latest":
    # Nexus supports /latest for platform packages but may not expose
    # /<resolved-version>. Use the tag URL when the user requested latest.
    print(f"${NPM_REGISTRY_URL%/}/opencode-linux-x64/latest")
else:
    print(f"${NPM_REGISTRY_URL%/}/opencode-linux-x64/{version}")
PY
  )"
  download_npm_tgz_to_cache "$OPENCODE_LINUX_X64_TGZ_BASENAME" "$opencode_linux_x64_meta_url"
else
  echo "[prepare] skip OpenCode npm cache (PREPARE_OPENCODE_CACHE=0)"
fi

manifest_packages="opik,uuid6,socksio,pip,setuptools,wheel,get-pip.py,python3.12-runtime.tar.gz,node-runtime.tar.xz,npm-cache,npm-cache-ready,@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
if [[ "$PREPARE_OPENCODE_CACHE" == "1" ]]; then
  manifest_packages="${manifest_packages},opencode-ai@${OPENCODE_VERSION},opencode-linux-x64@${OPENCODE_VERSION}"
fi
if [[ "$PREPARE_PI_CACHE" == "1" ]]; then
  manifest_packages="${manifest_packages},${PI_NODE_RUNTIME_BASENAME},${PI_RUNTIME_BASENAME},@earendil-works/pi-coding-agent@${PI_VERSION}"
fi

cat > "$WHEEL_DIR/manifest.txt" <<EOF
generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cache_schema=$CACHE_SCHEMA
python_bin=$PYTHON_BIN
claude_code_version=$CLAUDE_CODE_VERSION
opencode_version=$OPENCODE_VERSION
prepare_opencode_cache=$PREPARE_OPENCODE_CACHE
pi_version=$PI_VERSION
prepare_pi_cache=$PREPARE_PI_CACHE
pi_runtime_version=$(if [[ "$PREPARE_PI_CACHE" == "1" ]]; then printf '%s' "$PI_VERSION"; fi)
claude_npm_cache_version=$CLAUDE_CODE_VERSION
local_deps_minimal=false
packages=$manifest_packages
EOF

echo "[prepare] done"
