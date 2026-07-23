"""Harbor-side OpenCode agent wrapper for realtime Opik tracing.

`OpikOpenCodeHarbor` subclasses `harbor.agents.installed.opencode.OpenCode`.
It wraps install with retry handling, installs the realtime plugin files, and
keeps the run path compatible with custom provider names while rewriting host
localhost Opik URLs to `host.docker.internal` inside task containers.

Activated via:
    --agent-import-path opik_opencode_harbor:OpikOpenCodeHarbor

The realtime hook itself runs entirely inside the container (TS plugin
+ python hook reading opencode's SQLite DB).
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
TRACE_PLUGIN_SOURCE_DIR = Path(
    os.environ.get(
        "TRACE_PLUGIN_SOURCE_DIR",
        REPO_ROOT / "third_party" / "agent-opik-plugin",
    )
).expanduser()
PLUGIN_TS = Path(
    os.environ.get(
        "TRACE_PLUGIN_OPENCODE_PLUGIN_SOURCE",
        TRACE_PLUGIN_SOURCE_DIR / "harness" / "opencode" / "opik-trace.ts",
    )
).expanduser()
HOOK_PY = Path(
    os.environ.get(
        "TRACE_PLUGIN_OPENCODE_HOOK_SOURCE",
        TRACE_PLUGIN_SOURCE_DIR
        / "src"
        / "sii_opik_plugin"
        / "opencode"
        / "opencode_realtime_trace.py",
    )
).expanduser()
FINALIZER_PY = ROOT / "finalize_opencode_sessions.py"

CONTAINER_PLUGIN_REL = ".config/opencode/plugins"
CONTAINER_STATE_REL = ".opencode/state"


def _trace_to_opik_enabled(extra_env: dict[str, str] | None = None) -> bool:
    value: str | None = None
    if extra_env is not None:
        value = extra_env.get("TRACE_TO_OPIK")
    if value is None:
        value = os.environ.get("TRACE_TO_OPIK", "true")
    return value not in {"false", "0"}


# ── url helpers ───────────────────────────────────────────────────────────────


def _rewrite_container_proxy(value: str) -> str:
    """Map `127.0.0.1` / `localhost` in a URL netloc to `host.docker.internal`.
    Other URLs returned unchanged. Malformed inputs returned as-is."""
    try:
        parts = urlsplit(value)
    except Exception:
        return value

    hostname = parts.hostname
    if hostname not in {"127.0.0.1", "localhost"}:
        return value

    netloc = parts.netloc
    if "@" in netloc:
        auth, hostpart = netloc.rsplit("@", 1)
        hostpart = hostpart.replace(hostname, "host.docker.internal", 1)
        netloc = f"{auth}@{hostpart}"
    else:
        netloc = netloc.replace(hostname, "host.docker.internal", 1)

    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def _rewrite_container_opik_url(value: str) -> str:
    value = _rewrite_container_proxy(value)
    try:
        parts = urlsplit(value)
    except Exception:
        return value
    if parts.port == 5173 and parts.path in {"", "/"}:
        return urlunsplit(
            (parts.scheme, parts.netloc, "/api/", parts.query, parts.fragment)
        )
    if parts.port == 5173 and parts.path == "/api":
        return urlunsplit(
            (parts.scheme, parts.netloc, "/api/", parts.query, parts.fragment)
        )
    return value


# ── cold-path retry helper ────────────────────────────────────────────────────


async def _retry_async(
    label: str,
    runner: Callable[[], Awaitable[None]],
    attempts: int = 3,
    backoff_base_s: float = 5.0,
    backoff_cap_s: float = 60.0,
    sanity_check: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Retry an async install step with exponential backoff.

    Used by ``OpikOpenCodeHarbor.install`` to make the cold path tolerant of
    transient network failures (apt mirror flap, PyPI hiccup, GitHub fetch for
    nvm). If ``sanity_check`` returns True — either before the first attempt or
    after a failed one — the step is treated as success without further
    retries. This lets us forgive partially-installed upstream tooling: nvm and
    ``npm i -g opencode-ai`` are idempotent enough that a second invocation may
    exit non-zero on "already exists" while the binary is in fact present.

    Backoff for failure on attempt N (1-indexed) is
    ``min(backoff_cap_s, backoff_base_s * 2 ** (N - 1))``. The final attempt's
    failure re-raises the underlying exception unchanged.
    """

    async def _check_sanity() -> bool:
        if sanity_check is None:
            return False
        try:
            return bool(await sanity_check())
        except Exception:
            return False

    if await _check_sanity():
        print(f"[opik-cold] {label}: skipped (sanity check already passes)", flush=True)
        return

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            await runner()
            print(f"[opik-cold] {label}: ok on attempt {attempt}/{attempts}", flush=True)
            return
        except Exception as exc:
            last_exc = exc
            print(
                f"[opik-cold] {label}: attempt {attempt}/{attempts} failed: "
                f"{exc.__class__.__name__}: {exc}",
                flush=True,
            )
            if await _check_sanity():
                print(
                    f"[opik-cold] {label}: post-failure sanity check passed, "
                    f"treating as success",
                    flush=True,
                )
                return
            if attempt < attempts:
                delay = min(backoff_cap_s, backoff_base_s * (2 ** (attempt - 1)))
                print(f"[opik-cold] {label}: sleeping {delay:.1f}s before retry", flush=True)
                await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


# ── agent subclass ────────────────────────────────────────────────────────────


class OpikOpenCodeHarbor(OpenCode):
    """OpenCode agent with realtime Opik tracing layered on.

    Overrides:
      * `install` — install Python/OpenCode/Opik hook deps using the same
        cache-first order as the Claude Code path: mounted local cache, local
        wheel HTTP URL, then package-manager / public registry fallback.
      * `run` — copy of upstream `OpenCode.run` logic with the hardcoded
        provider whitelist removed (so `provider=custom` works) and the
        OPIK_URL host rewritten from localhost to host.docker.internal
        before opencode is invoked.
    """

    async def install(self, environment: BaseEnvironment) -> None:
        async def _prepare_python_runtime() -> None:
            # Do not run apt unless Python is actually missing. Several Seta
            # images intentionally contain broken dpkg state; a no-op apt
            # install still reconfigures those packages and breaks setup.
            await self.exec_as_root(
                environment,
                command=(
                    "set -euo pipefail; "
                    "wheel_dir=\"${CC_OPIK_PY_WHEEL_DIR:-/opt/tb-opik/python-wheels}\"; "
                    # Some task images contain a python3 wrapper whose target
                    # is missing. Treat Python as present only if it executes.
                    "if command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1\n"
                    "import sys\n"
                    "print(sys.version)\n"
                    "PY\n"
                    "then exit 0; fi; "
                    "if [ -f \"$wheel_dir/python3.12-runtime.tar.gz\" ] && command -v tar >/dev/null 2>&1; then "
                      "  rm -rf /opt/python3.12-runtime; "
                      "  mkdir -p /opt; "
                      "  tar -xzf \"$wheel_dir/python3.12-runtime.tar.gz\" -C /opt; "
                    "  if [ -x /opt/python3.12-runtime/bin/python3.12 ] "
                    "    && /opt/python3.12-runtime/bin/python3.12 - <<'PY' >/dev/null 2>&1\n"
                    "import sys\n"
                    "print(sys.version)\n"
                    "PY\n"
                    "  then "
                    # Do not symlink the cached wrapper. It derives runtime
                    # paths from $0, so a /usr/local/bin symlink makes it look
                    # for /usr/local/bin/python3.12.real and breaks hooks.
                    "    printf '%s\n' '#!/bin/sh' 'exec /opt/python3.12-runtime/bin/python3.12 \"$@\"' > /usr/local/bin/python3; "
                    "    printf '%s\n' '#!/bin/sh' 'exec /opt/python3.12-runtime/bin/python3.12 \"$@\"' > /usr/local/bin/python3.12; "
                    "    chmod +x /usr/local/bin/python3 /usr/local/bin/python3.12; "
                      "    exit 0; "
                      "  fi; "
                    # A corrupt cached runtime leaves a wrapper that points at
                    # a missing python3.12.real. Remove it so later hook startup
                    # cannot silently spawn a broken /usr/local/bin/python3.
                    "  rm -rf /opt/python3.12-runtime; "
                    "  rm -f /usr/local/bin/python3 /usr/local/bin/python3.12; "
                    "fi; "
                    "if command -v apk >/dev/null 2>&1; then "
                    "  apk add --no-cache python3 py3-pip; "
                    "elif command -v apt-get >/dev/null 2>&1; then "
                    "  apt-get update && apt-get install -y python3 python3-pip; "
                    "elif command -v yum >/dev/null 2>&1; then "
                    "  yum install -y python3 python3-pip; "
                    "else "
                    "  echo '[WARN] no known package manager for python install' >&2; "
                    "fi; "
                    # Keep later agent commands away from broken /usr/local/bin
                    # wrappers by pointing python3 at the package-manager copy.
                    "if [ -x /usr/bin/python3 ] && /usr/bin/python3 - <<'PY' >/dev/null 2>&1\n"
                    "import sys\n"
                    "print(sys.version)\n"
                    "PY\n"
                    "then ln -sf /usr/bin/python3 /usr/local/bin/python3; fi; "
                    # The realtime plugin spawns `python3` with stderr hidden.
                    # Fail install here instead of losing all opencode traces.
                    "python3 - <<'PY' >/dev/null\n"
                    "import sys\n"
                    "print(sys.version)\n"
                    "PY\n"
                ),
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )

        await _retry_async(
            "prepare python runtime",
            _prepare_python_runtime,
            attempts=3,
            backoff_base_s=5.0,
        )

        async def _opencode_present() -> bool:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        "export PATH=\"$HOME/.local/bin:$PATH\"; "
                        "node --version >/dev/null && opencode --version >/dev/null"
                    ),
                )
                return True
            except Exception:
                return False

        async def _run_local_opencode_install() -> None:
            # Prefer the monitor-prepared Node/OpenCode cache. This avoids 80
            # task containers all fetching nvm, nodejs.org, and npm in parallel.
            raw_version = getattr(self, "version", None)
            if callable(raw_version):
                raw_version = raw_version()
            version = str(raw_version or os.environ.get("OPENCODE_VERSION", "latest"))
            version_q = shlex.quote(version)
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "export PATH=\"$HOME/.local/bin:$PATH\"; "
                    f"opencode_version={version_q}; "
                    "download_file() { "
                    "  url=\"$1\"; dest=\"$2\"; "
                    "  if command -v curl >/dev/null 2>&1; then curl -fsSL \"$url\" -o \"$dest\"; "
                    "  elif command -v wget >/dev/null 2>&1; then wget -qO \"$dest\" \"$url\"; "
                    "  elif command -v python3 >/dev/null 2>&1; then python3 - <<'PY' \"$url\" \"$dest\"\n"
                    "import sys, urllib.request\n"
                    "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])\n"
                    "PY\n"
                    "  else return 1; fi; "
                    "}; "
                    "extract_archive() { "
                    "  archive=\"$1\"; dest=\"$2\"; "
                    "  mkdir -p \"$dest\"; "
                    "  if command -v tar >/dev/null 2>&1 && tar -xf \"$archive\" -C \"$dest\"; then return 0; fi; "
                    "  if command -v python3 >/dev/null 2>&1; then python3 - <<'PY' \"$archive\" \"$dest\"\n"
                    "import sys, tarfile\n"
                    "with tarfile.open(sys.argv[1]) as archive:\n"
                    "    archive.extractall(sys.argv[2])\n"
                    "PY\n"
                    "  else return 1; fi; "
                    "}; "
                    "wheel_dir=\"${CC_OPIK_PY_WHEEL_DIR:-/opt/tb-opik/python-wheels}\"; "
                    "wheel_url=\"${TB_LOCAL_WHEEL_SERVER_URL:-}\"; "
                    "node_tgz=\"$wheel_dir/node-runtime.tar.xz\"; "
                    "opencode_tgz=\"${OPENCODE_TGZ_PATH:-}\"; "
                    "opencode_linux_x64_tgz=\"${OPENCODE_LINUX_X64_TGZ_PATH:-}\"; "
                    "opencode_name=\"opencode-ai-${opencode_version}.tgz\"; "
                    "opencode_linux_x64_name=\"opencode-linux-x64-${opencode_version}.tgz\"; "
                    "if [ -z \"$opencode_tgz\" ]; then opencode_tgz=\"$wheel_dir/$opencode_name\"; fi; "
                    "if [ -z \"$opencode_linux_x64_tgz\" ]; then opencode_linux_x64_tgz=\"$wheel_dir/$opencode_linux_x64_name\"; fi; "
                    "if [ ! -f \"$opencode_tgz\" ] && [ -n \"${TB_LOCAL_OPENCODE_TGZ_URL:-}\" ]; then "
                    "  tmp_tgz=\"$(mktemp /tmp/opencode-ai-XXXXXX.tgz)\"; "
                    "  download_file \"$TB_LOCAL_OPENCODE_TGZ_URL\" \"$tmp_tgz\" >/dev/null 2>&1 || true; "
                    "  if [ -s \"$tmp_tgz\" ]; then opencode_tgz=\"$tmp_tgz\"; fi; "
                    "fi; "
                    "if [ ! -f \"$opencode_tgz\" ] && [ -n \"$wheel_url\" ]; then "
                    "  tmp_tgz=\"$(mktemp /tmp/opencode-ai-XXXXXX.tgz)\"; "
                    "  download_file \"${wheel_url%/}/$opencode_name\" \"$tmp_tgz\" >/dev/null 2>&1 || true; "
                    "  if [ -s \"$tmp_tgz\" ]; then opencode_tgz=\"$tmp_tgz\"; fi; "
                    "fi; "
                    "if [ ! -f \"$opencode_linux_x64_tgz\" ] && [ -n \"${TB_LOCAL_OPENCODE_LINUX_X64_TGZ_URL:-}\" ]; then "
                    "  tmp_platform_tgz=\"$(mktemp /tmp/opencode-linux-x64-XXXXXX.tgz)\"; "
                    "  download_file \"$TB_LOCAL_OPENCODE_LINUX_X64_TGZ_URL\" \"$tmp_platform_tgz\" >/dev/null 2>&1 || true; "
                    "  if [ -s \"$tmp_platform_tgz\" ]; then opencode_linux_x64_tgz=\"$tmp_platform_tgz\"; fi; "
                    "fi; "
                    "if [ ! -f \"$opencode_linux_x64_tgz\" ] && [ -n \"$wheel_url\" ]; then "
                    "  tmp_platform_tgz=\"$(mktemp /tmp/opencode-linux-x64-XXXXXX.tgz)\"; "
                    "  download_file \"${wheel_url%/}/$opencode_linux_x64_name\" \"$tmp_platform_tgz\" >/dev/null 2>&1 || true; "
                    "  if [ -s \"$tmp_platform_tgz\" ]; then opencode_linux_x64_tgz=\"$tmp_platform_tgz\"; fi; "
                    "fi; "
                    "mkdir -p \"$HOME/.local/bin\"; "
                    "if ! command -v npm >/dev/null 2>&1 && [ -f \"$node_tgz\" ]; then "
                    "  node_dir=\"$(mktemp -d /tmp/tb-node-XXXXXX)\"; "
                    "  extract_archive \"$node_tgz\" \"$node_dir\"; "
                    "  node_bin=\"$(find \"$node_dir\" -path '*/bin/npm' -print -quit 2>/dev/null)\"; "
                    "  if [ -n \"$node_bin\" ]; then "
                    "    node_runtime_bin=\"$(dirname \"$node_bin\")\"; "
                    "    mkdir -p \"$HOME/.local/bin\"; "
                    "    ln -sf \"$node_runtime_bin/node\" \"$HOME/.local/bin/node\" 2>/dev/null || true; "
                    "    ln -sf \"$node_runtime_bin/npm\" \"$HOME/.local/bin/npm\" 2>/dev/null || true; "
                    "    ln -sf \"$node_runtime_bin/npx\" \"$HOME/.local/bin/npx\" 2>/dev/null || true; "
                    "    export PATH=\"$HOME/.local/bin:$node_runtime_bin:$PATH\"; "
                    "  fi; "
                    "fi; "
                    "if ! command -v npm >/dev/null 2>&1; then "
                    "  if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y nodejs npm; "
                    "  elif command -v apk >/dev/null 2>&1; then apk add --no-cache nodejs npm; "
                    "  elif command -v yum >/dev/null 2>&1; then yum install -y nodejs npm; fi; "
                    "fi; "
                    "npm config set prefix \"$HOME/.local\" >/dev/null 2>&1 || true; "
                    "use_linux_x64_platform=0; "
                    # Only use the cached glibc x64 binary on matching images.
                    # Alpine/musl keeps the existing npm fallback so optional
                    # package selection can pick the compatible binary.
                    "if [ \"$(uname -m 2>/dev/null)\" = \"x86_64\" ] "
                    "  && command -v ldd >/dev/null 2>&1 "
                    "  && ldd --version 2>&1 | grep -qi 'glibc\\|GNU libc' "
                    "  && [ -f \"$opencode_linux_x64_tgz\" ]; then "
                    "  use_linux_x64_platform=1; "
                    "fi; "
                    "if [ -f \"$opencode_tgz\" ]; then "
                    "  if [ \"$use_linux_x64_platform\" = \"1\" ]; then "
                    "    npm install -g \"$opencode_tgz\" \"$opencode_linux_x64_tgz\" && opencode --version; "
                    "  else "
                    "    npm install -g \"$opencode_tgz\" && opencode --version; "
                    "  fi "
                    "    || { npm install -g \"opencode-ai@${opencode_version}\"; opencode --version; }; "
                    "else "
                    "  npm install -g \"opencode-ai@${opencode_version}\"; "
                    "  opencode --version; "
                    "fi"
                ),
            )

        await _retry_async(
            "local opencode install",
            _run_local_opencode_install,
            attempts=2,
            backoff_base_s=10.0,
            sanity_check=_opencode_present,
        )

        if not _trace_to_opik_enabled(getattr(self, "_extra_env", None)):
            print(
                "[opik-cold] TRACE_TO_OPIK=false: skip Opik hook dependencies "
                "and plugin files",
                flush=True,
            )
            return

        async def _run_hook_python_deps_install() -> None:
            # Keep the runtime install offline/cache-first. Only fall back to
            # public pip when neither the mounted cache nor the wheel HTTP
            # mirror is usable.
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "py_bin=\"\"; "
                    "for candidate in /opt/python3.12-runtime/bin/python3.12 python3.12 python3; do "
                    "  ([ -x \"$candidate\" ] || command -v \"$candidate\" >/dev/null 2>&1) || continue; "
                    "  \"$candidate\" - <<'PY' >/dev/null 2>&1 || continue\n"
                    "import sys\n"
                    "print(sys.version)\n"
                    "PY\n"
                    "  py_bin=\"$candidate\"; break; "
                    "done; "
                    "if [ -z \"$py_bin\" ]; then echo '[WARN] python missing for opik hook deps' >&2; exit 1; fi; "
                    "wheel_dir=\"${CC_OPIK_PY_WHEEL_DIR:-/opt/tb-opik/python-wheels}\"; "
                    "wheel_url=\"${TB_LOCAL_WHEEL_SERVER_URL:-}\"; "
                    "missing=$(\"$py_bin\" - <<'PY'\n"
                    "import importlib.util\n"
                    "mods = ('opik', 'uuid6', 'socksio')\n"
                    "print(' '.join(m for m in mods if importlib.util.find_spec(m) is None))\n"
                    "PY\n"
                    "); "
                    "if [ -z \"$missing\" ]; then exit 0; fi; "
                    # Debian/Ubuntu task images often enable PEP 668. Set the
                    # env override unconditionally so cached wheel installs do
                    # not fail agent setup before the hook can be installed.
                    "export PIP_BREAK_SYSTEM_PACKAGES=1; "
                    "pip_opts=\"\"; "
                    "if [ -d \"$wheel_dir\" ]; then "
                    "  pip_opts=\"--no-index --find-links $wheel_dir\"; "
                    "elif [ -n \"$wheel_url\" ]; then "
                    "  trusted_host=\"$(printf %s \"$wheel_url\" | sed -E 's#^https?://([^/:]+).*#\\1#')\"; "
                    "  pip_opts=\"--trusted-host $trusted_host --no-index --find-links $wheel_url\"; "
                    "fi; "
                    "if ! \"$py_bin\" -m pip --version >/dev/null 2>&1; then "
                    "  if [ -f \"$wheel_dir/get-pip.py\" ]; then "
                    "    \"$py_bin\" \"$wheel_dir/get-pip.py\" --user $pip_opts pip setuptools wheel >/dev/null 2>&1 "
                    "      || \"$py_bin\" \"$wheel_dir/get-pip.py\" --break-system-packages $pip_opts pip setuptools wheel >/dev/null 2>&1 "
                    "      || true; "
                    "  elif [ -n \"$wheel_url\" ]; then "
                    "    tmp_get_pip=\"$(mktemp /tmp/get-pip-XXXXXX.py)\"; "
                    "    if command -v curl >/dev/null 2>&1; then curl -fsSL \"${wheel_url%/}/get-pip.py\" -o \"$tmp_get_pip\"; "
                    "    elif command -v wget >/dev/null 2>&1; then wget -qO \"$tmp_get_pip\" \"${wheel_url%/}/get-pip.py\"; "
                    "    elif command -v python3 >/dev/null 2>&1; then python3 - <<'PY' \"${wheel_url%/}/get-pip.py\" \"$tmp_get_pip\" >/dev/null 2>&1 || true\n"
                    "import sys, urllib.request\n"
                    "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])\n"
                    "PY\n"
                    "    fi; "
                    "    if [ -s \"$tmp_get_pip\" ]; then "
                    "      \"$py_bin\" \"$tmp_get_pip\" --user $pip_opts pip setuptools wheel >/dev/null 2>&1 "
                    "        || \"$py_bin\" \"$tmp_get_pip\" --break-system-packages $pip_opts pip setuptools wheel >/dev/null 2>&1 "
                    "        || true; "
                    "    fi; "
                    "    rm -f \"$tmp_get_pip\"; "
                    "  fi; "
                    "fi; "
                    "break_opt=\"\"; "
                    "if \"$py_bin\" -m pip install --help 2>/dev/null | grep -q -- '--break-system-packages'; then "
                    "  break_opt=\"--break-system-packages\"; "
                    "fi; "
                    "\"$py_bin\" -m pip install --retries 10 --timeout 120 $break_opt --ignore-installed $pip_opts $missing "
                    "|| \"$py_bin\" -m pip install --retries 10 --timeout 120 --ignore-installed $pip_opts $missing "
                    "|| \"$py_bin\" -m pip install --retries 10 --timeout 120 --user --ignore-installed $pip_opts $missing "
                    "|| \"$py_bin\" -m pip install --retries 10 --timeout 120 $break_opt --ignore-installed $missing "
                    "|| \"$py_bin\" -m pip install --retries 10 --timeout 120 --user --ignore-installed $missing "
                    "|| { echo '[WARN] failed to install python deps for opik hook' >&2; exit 1; }"
                ),
            )

        await _retry_async(
            "pip install opik+uuid6+socksio",
            _run_hook_python_deps_install,
            attempts=2,
            backoff_base_s=10.0,
        )

        # Stage the plugin files in /tmp first (no `~` in upload_file
        # target, see DockerEnvironment.upload_file → docker compose cp,
        # which does not expand `~`), then install into $HOME via a
        # shell command that resolves $HOME inside the container.
        await environment.upload_file(PLUGIN_TS, "/tmp/opik-trace.ts")
        await environment.upload_file(HOOK_PY, "/tmp/opencode_realtime_trace.py")
        await environment.upload_file(FINALIZER_PY, "/tmp/finalize_opencode_sessions.py")
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f'mkdir -p "$HOME/{CONTAINER_PLUGIN_REL}" "$HOME/{CONTAINER_STATE_REL}"; '
                "install -m 0644 /tmp/opik-trace.ts "
                f'"$HOME/{CONTAINER_PLUGIN_REL}/opik-trace.ts"; '
                "install -m 0755 /tmp/opencode_realtime_trace.py "
                f'"$HOME/{CONTAINER_PLUGIN_REL}/opencode_realtime_trace.py"; '
                "install -m 0755 /tmp/finalize_opencode_sessions.py "
                f'"$HOME/{CONTAINER_PLUGIN_REL}/finalize_opencode_sessions.py"'
            ),
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        escaped_instruction = shlex.quote(instruction)
        trace_enabled = _trace_to_opik_enabled(getattr(self, "_extra_env", None))

        if not self.model_name:
            raise ValueError("Model name must not be empty")

        env: dict[str, str] = {}

        # Replace upstream's provider-specific env handling with explicit
        # forwarding of everything the runner already injected via `--ae`.
        # `_extra_env` is also merged inside `_exec`, so this is mostly for
        # clarity.
        for key, value in self._extra_env.items():
            env[key] = value

        if trace_enabled:
            # Localhost OPIK_URL on the host needs to become
            # host.docker.internal inside the container, otherwise the
            # in-container hook can't reach the local Opik backend.
            for key in ("OPIK_URL", "OPIK_URL_OVERRIDE"):
                if key in env:
                    env[key] = _rewrite_container_opik_url(env[key])

        env["OPENCODE_FAKE_VCS"] = "git"
        if trace_enabled:
            # Harbor only downloads EnvironmentPaths.agent_dir after timeout.
            # Keep the hook runtime backup there so the outer worker can replay
            # the normal finalizer instead of a simplified timeout trace.
            env.setdefault("OC_OPIK_LOGS_DIR", "/logs/agent")

        # Keep opencode realtime traces independent, matching the Claude hook
        # shape: one agent session owns one Opik trace/thread. Do not forward
        # Harbor's current trace/span IDs here; the hook persists its own
        # trace_id and uses opencode's session id only to read the local DB.

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command, env=env)

        config_command = self._build_register_config_command()
        if config_command:
            await self.exec_as_agent(environment, command=config_command, env=env)

        if trace_enabled:
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "python3 - <<'PY'\n"
                    "import json\n"
                    "from pathlib import Path\n"
                    "cfg_path = Path.home() / '.config/opencode/opencode.json'\n"
                    "plugin_path = str(Path.home() / '.config/opencode/plugins/opik-trace.ts')\n"
                    "data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}\n"
                    "plugins = data.get('plugin')\n"
                    "if not isinstance(plugins, list):\n"
                    "    plugins = []\n"
                    "if plugin_path not in plugins:\n"
                    "    plugins.append(plugin_path)\n"
                    "data['plugin'] = plugins\n"
                    "cfg_path.parent.mkdir(parents=True, exist_ok=True)\n"
                    "cfg_path.write_text(json.dumps(data, indent=2))\n"
                    "PY"
                ),
                env=env,
            )

        finalize_command = ""
        if trace_enabled:
            finalize_command = (
                f'python3 "$HOME/{CONTAINER_PLUGIN_REL}/finalize_opencode_sessions.py" '
                ">>/logs/agent/opencode.txt 2>&1 || true; "
            )

        await self.exec_as_agent(
            environment,
            command=(
                "set -o pipefail; "
                "export PATH=\"$HOME/.local/bin:$PATH\"; "
                ". ~/.nvm/nvm.sh 2>/dev/null || true; "
                f"opencode --model={self.model_name} run --format=json --thinking "
                f"--dangerously-skip-permissions -- {escaped_instruction} "
                f"2>&1 </dev/null | stdbuf -oL tee /logs/agent/opencode.txt; "
                # opencode does not consistently emit a terminal plugin event
                # under Harbor. Keep finalization best-effort, but return the
                # original opencode status so Harbor retries/accounting still work.
                "opencode_rc=$?; "
                f"{finalize_command}"
                "exit \"$opencode_rc\""
            ),
            env=env,
        )
