"""Build shared shell commands for Harbor agent container setup."""

from __future__ import annotations

import shlex
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class NpmToolSpec:
    executable: str
    package: str
    version: str
    archive_path: str
    archive_url: str = ""
    archive_basename: str = ""
    platform_archive_path: str = ""
    platform_archive_url: str = ""
    platform_archive_basename: str = ""
    npm_cache_dir: str = ""
    npm_registry: str = ""


def build_python_runtime_command(
    wheel_dir: str, *, python_required: bool = True
) -> str:
    wheel_dir_q = shlex.quote(wheel_dir)
    missing_python_exit_code = 1 if python_required else 0
    missing_python_message = (
        "[ERROR] python missing after runtime setup"
        if python_required
        else "[WARN] python missing, skip opik hook deps"
    )
    return textwrap.dedent(
        rf"""
        set -euo pipefail
        wheel_dir={wheel_dir_q}
        python_works() {{
          "$1" - <<'PY' >/dev/null 2>&1
        import sys
        print(sys.version)
        PY
        }}
        if python_works /opt/python3.12-runtime/bin/python3.12; then
          exit 0
        fi
        runtime_tgz="$wheel_dir/python3.12-runtime.tar.gz"
        if [ -f "$runtime_tgz" ] && command -v tar >/dev/null 2>&1; then
          rm -rf /opt/python3.12-runtime
          mkdir -p /opt
          if tar -xzf "$runtime_tgz" -C /opt \
            && python_works /opt/python3.12-runtime/bin/python3.12; then
            printf '%s\n' '#!/bin/sh' \
              'exec /opt/python3.12-runtime/bin/python3.12 "$@"' \
              > /usr/local/bin/python3
            printf '%s\n' '#!/bin/sh' \
              'exec /opt/python3.12-runtime/bin/python3.12 "$@"' \
              > /usr/local/bin/python3.12
            chmod +x /usr/local/bin/python3 /usr/local/bin/python3.12
            exit 0
          fi
          rm -rf /opt/python3.12-runtime
          rm -f /usr/local/bin/python3 /usr/local/bin/python3.12
        fi
        if python_works python3; then
          exit 0
        fi
        if command -v apk >/dev/null 2>&1; then
          apk add --no-cache python3 py3-pip
        elif command -v apt-get >/dev/null 2>&1; then
          apt-get -o Acquire::ForceIPv4=true update
          apt-get -o Acquire::ForceIPv4=true install -y python3 python3-pip
        elif command -v yum >/dev/null 2>&1; then
          yum install -y python3 python3-pip
        else
          echo '[WARN] no known package manager for python install' >&2
        fi
        if [ -x /usr/bin/python3 ] && python_works /usr/bin/python3; then
          ln -sf /usr/bin/python3 /usr/local/bin/python3
        fi
        if ! python_works python3; then
          echo {shlex.quote(missing_python_message)} >&2
          exit {missing_python_exit_code}
        fi
        """
    ).lstrip()


def build_npm_tool_install_command(
    spec: NpmToolSpec,
    *,
    wheel_dir: str,
    wheel_url: str,
    node_dist_url: str,
) -> str:
    wheel_dir_q = shlex.quote(wheel_dir)
    wheel_url_q = shlex.quote(wheel_url)
    node_dist_url_q = shlex.quote(node_dist_url)
    executable_q = shlex.quote(spec.executable)
    archive_path_q = shlex.quote(spec.archive_path)
    archive_url_q = shlex.quote(spec.archive_url)
    archive_basename_q = shlex.quote(spec.archive_basename)
    platform_path_q = shlex.quote(spec.platform_archive_path)
    platform_url_q = shlex.quote(spec.platform_archive_url)
    platform_basename_q = shlex.quote(spec.platform_archive_basename)
    npm_cache_dir_q = shlex.quote(spec.npm_cache_dir)
    npm_registry_q = shlex.quote(spec.npm_registry)
    registry_spec = shlex.quote(
        f"{spec.package}@{spec.version}" if spec.version else spec.package
    )

    return textwrap.dedent(
        rf"""
        set -euo pipefail
        export PATH="$HOME/.local/bin:$PATH"
        wheel_dir={wheel_dir_q}
        wheel_url={wheel_url_q}
        node_dist_url={node_dist_url_q}
        tool_executable={executable_q}
        tool_tgz={archive_path_q}
        tool_tgz_url={archive_url_q}
        tool_tgz_basename={archive_basename_q}
        platform_tgz={platform_path_q}
        platform_tgz_url={platform_url_q}
        platform_tgz_basename={platform_basename_q}
        npm_cache_dir={npm_cache_dir_q}
        npm_registry={npm_registry_q}
        download_file() {{
          url="$1"
          dest="$2"
          if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$url" -o "$dest"
          elif command -v wget >/dev/null 2>&1; then
            wget -qO "$dest" "$url"
          elif command -v python3 >/dev/null 2>&1; then
            python3 - "$url" "$dest" <<'PY'
        import sys, urllib.request
        urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
        PY
          else
            return 1
          fi
        }}
        extract_archive() {{
          archive="$1"
          dest="$2"
          mkdir -p "$dest"
          if command -v tar >/dev/null 2>&1 \
            && tar -xf "$archive" -C "$dest"; then
            return 0
          fi
          if command -v python3 >/dev/null 2>&1; then
            if python3 - "$archive" "$dest" <<'PY'
        import sys, tarfile
        with tarfile.open(sys.argv[1]) as archive:
            archive.extractall(sys.argv[2])
        PY
            then
              return 0
            fi
          fi
          return 1
        }}
        activate_node_runtime() {{
          node_dir="$1"
          node_bin="$(find "$node_dir" -path '*/bin/npm' -print -quit 2>/dev/null)"
          if [ -z "$node_bin" ]; then
            return 1
          fi
          node_runtime_bin="$(dirname "$node_bin")"
          mkdir -p "$HOME/.local/bin"
          ln -sf "$node_runtime_bin/node" "$HOME/.local/bin/node" 2>/dev/null || true
          ln -sf "$node_runtime_bin/npm" "$HOME/.local/bin/npm" 2>/dev/null || true
          ln -sf "$node_runtime_bin/npx" "$HOME/.local/bin/npx" 2>/dev/null || true
          export PATH="$HOME/.local/bin:$node_runtime_bin:$PATH"
        }}
        fetch_archive() {{
          current="$1"
          explicit_url="$2"
          basename="$3"
          tmp_pattern="$4"
          if [ -f "$current" ]; then
            printf '%s\n' "$current"
            return 0
          fi
          url="$explicit_url"
          if [ -z "$url" ] && [ -n "$wheel_url" ] && [ -n "$basename" ]; then
            url="${{wheel_url%/}}/$basename"
          fi
          if [ -z "$url" ]; then
            printf '%s\n' "$current"
            return 0
          fi
          downloaded="$(mktemp "$tmp_pattern")"
          if download_file "$url" "$downloaded" >/dev/null 2>&1 \
            && [ -s "$downloaded" ]; then
            printf '%s\n' "$downloaded"
          else
            printf '%s\n' "$current"
          fi
        }}
        tool_tgz="$(fetch_archive "$tool_tgz" "$tool_tgz_url" \
          "$tool_tgz_basename" /tmp/tb-tool-XXXXXX.tgz)"
        if [ -n "$platform_tgz" ] || [ -n "$platform_tgz_url" ] \
          || [ -n "$platform_tgz_basename" ]; then
          platform_tgz="$(fetch_archive "$platform_tgz" "$platform_tgz_url" \
            "$platform_tgz_basename" /tmp/tb-platform-XXXXXX.tgz)"
        fi
        mkdir -p "$HOME/.local/bin"
        node_tgz="$wheel_dir/node-runtime.tar.xz"
        if ! command -v npm >/dev/null 2>&1 && [ -f "$node_tgz" ]; then
          node_dir="$(mktemp -d /tmp/tb-node-XXXXXX)"
          if extract_archive "$node_tgz" "$node_dir"; then
            activate_node_runtime "$node_dir" || true
          fi
        fi
        if ! command -v npm >/dev/null 2>&1 && [ -n "$node_dist_url" ]; then
          node_dist_tgz="$(mktemp /tmp/tb-node-dist-XXXXXX.tgz)"
          if download_file "$node_dist_url" "$node_dist_tgz" && [ -s "$node_dist_tgz" ]; then
            node_dir="$(mktemp -d /tmp/tb-node-XXXXXX)"
            if extract_archive "$node_dist_tgz" "$node_dir"; then
              activate_node_runtime "$node_dir" || true
            fi
          fi
        fi
        if ! command -v npm >/dev/null 2>&1; then
          if command -v apt-get >/dev/null 2>&1; then
            apt-get -o Acquire::ForceIPv4=true update -qq
            apt-get -o Acquire::ForceIPv4=true install -y -qq nodejs npm
          elif command -v apk >/dev/null 2>&1; then
            apk add --no-cache nodejs npm bash curl
          elif command -v yum >/dev/null 2>&1; then
            yum install -y nodejs npm
          fi
        fi
        npm config set prefix "$HOME/.local" >/dev/null 2>&1 || true
        tool_check() {{
          hash -r 2>/dev/null || true
          for attempt in 1 2 3; do
            if command -v "$tool_executable" >/dev/null 2>&1 \
              && "$tool_executable" --version; then
              return 0
            fi
            sleep 1
          done
          return 1
        }}
        use_platform_archive=0
        if [ -n "$platform_tgz" ] \
          && [ "$(uname -m 2>/dev/null)" = "x86_64" ] \
          && command -v ldd >/dev/null 2>&1 \
          && ldd --version 2>&1 | grep -qi 'glibc\|GNU libc' \
          && [ -f "$platform_tgz" ]; then
          use_platform_archive=1
        fi
        tool_installed=0
        if command -v npm >/dev/null 2>&1 && [ -f "$tool_tgz" ]; then
          if [ -d "$npm_cache_dir" ]; then
            npm_cache_tmp="$(mktemp -d /tmp/tb-npm-cache-XXXXXX)"
            if cp -a "$npm_cache_dir"/. "$npm_cache_tmp"/; then
              if [ "$use_platform_archive" = 1 ]; then
                npm install -g --offline --cache "$npm_cache_tmp" \
                  "$tool_tgz" "$platform_tgz" && tool_check \
                  && tool_installed=1 || true
              else
                npm install -g --offline --cache "$npm_cache_tmp" \
                  "$tool_tgz" && tool_check && tool_installed=1 || true
              fi
            fi
          fi
          if [ "$tool_installed" != 1 ]; then
            if [ "$use_platform_archive" = 1 ]; then
              npm install -g "$tool_tgz" "$platform_tgz" && tool_check \
                && tool_installed=1 || true
            else
              npm install -g "$tool_tgz" && tool_check && tool_installed=1 || true
            fi
          fi
        fi
        if [ "$tool_installed" != 1 ]; then
          if [ -n "$npm_registry" ]; then
            NPM_CONFIG_REGISTRY="$npm_registry" \
              npm install -g {registry_spec}
          else
            npm install -g {registry_spec}
          fi
          tool_check
        fi
        """
    ).lstrip()


def build_python_dependencies_command(
    modules: tuple[str, ...],
    *,
    wheel_dir: str,
    wheel_url: str,
    python_required: bool = True,
) -> str:
    wheel_dir_q = shlex.quote(wheel_dir)
    wheel_url_q = shlex.quote(wheel_url)
    modules_repr = repr(tuple(modules))
    missing_python_exit_code = 1 if python_required else 0
    missing_python_message = (
        "[ERROR] python missing for hook dependencies"
        if python_required
        else "[WARN] python missing, skip opik hook deps"
    )
    return textwrap.dedent(
        rf"""
        set -euo pipefail
        py_bin=""
        for candidate in /opt/python3.12-runtime/bin/python3.12 python3.12 python3; do
          ([ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1) \
            || continue
          "$candidate" - <<'PY' >/dev/null 2>&1 || continue
        import sys
        print(sys.version)
        PY
          py_bin="$candidate"
          break
        done
        if [ -z "$py_bin" ]; then
          echo {shlex.quote(missing_python_message)} >&2
          exit {missing_python_exit_code}
        fi
        wheel_dir={wheel_dir_q}
        wheel_url={wheel_url_q}
        missing=$("$py_bin" - <<'PY'
        import importlib.util
        mods = {modules_repr}
        print(' '.join(module for module in mods if importlib.util.find_spec(module) is None))
        PY
        )
        if [ -z "$missing" ]; then
          exit 0
        fi
        export PIP_BREAK_SYSTEM_PACKAGES=1
        pip_opts=""
        if [ -d "$wheel_dir" ]; then
          pip_opts="--no-index --find-links $wheel_dir"
        elif [ -n "$wheel_url" ]; then
          trusted_host="$(printf %s "$wheel_url" \
            | sed -E 's#^https?://([^/:]+).*#\\1#')"
          pip_opts="--trusted-host $trusted_host --no-index --find-links $wheel_url"
        fi
        run_get_pip() {{
          get_pip="$1"
          "$py_bin" "$get_pip" --user $pip_opts pip setuptools wheel \
            >/dev/null 2>&1 \
            || "$py_bin" "$get_pip" --break-system-packages $pip_opts \
              pip setuptools wheel >/dev/null 2>&1 \
            || true
        }}
        if ! "$py_bin" -m pip --version >/dev/null 2>&1; then
          if [ -f "$wheel_dir/get-pip.py" ]; then
            run_get_pip "$wheel_dir/get-pip.py"
          elif [ -n "$wheel_url" ]; then
            tmp_get_pip="$(mktemp /tmp/get-pip-XXXXXX.py)"
            if command -v curl >/dev/null 2>&1; then
              curl -fsSL "${{wheel_url%/}}/get-pip.py" -o "$tmp_get_pip"
            elif command -v wget >/dev/null 2>&1; then
              wget -qO "$tmp_get_pip" "${{wheel_url%/}}/get-pip.py"
            elif command -v python3 >/dev/null 2>&1; then
              python3 - "${{wheel_url%/}}/get-pip.py" "$tmp_get_pip" \
                <<'PY' >/dev/null 2>&1 || true
        import sys, urllib.request
        urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
        PY
            fi
            if [ -s "$tmp_get_pip" ]; then
              run_get_pip "$tmp_get_pip"
            fi
            rm -f "$tmp_get_pip"
          fi
        fi
        break_opt=""
        if "$py_bin" -m pip install --help 2>/dev/null \
          | grep -q -- '--break-system-packages'; then
          break_opt="--break-system-packages"
        fi
        "$py_bin" -m pip install --retries 10 --timeout 120 \
          $break_opt --ignore-installed $pip_opts $missing \
          || "$py_bin" -m pip install --retries 10 --timeout 120 \
            --ignore-installed $pip_opts $missing \
          || "$py_bin" -m pip install --retries 10 --timeout 120 \
            --user --ignore-installed $pip_opts $missing \
          || "$py_bin" -m pip install --retries 10 --timeout 120 \
            $break_opt --ignore-installed $missing \
          || "$py_bin" -m pip install --retries 10 --timeout 120 \
            --user --ignore-installed $missing \
          || {{ echo '[WARN] failed to install python hook dependencies' >&2; exit 1; }}
        """
    ).lstrip()
