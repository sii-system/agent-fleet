# sitecustomize.py — Harbor × Opik monkey-patch module
#
# Python imports this file automatically before any user code runs, as long as
# the directory containing it appears on PYTHONPATH.  harboropik.sh prepends
# SCRIPT_DIR (the directory that contains this file) to PYTHONPATH, so this
# module is loaded by Harbor's Python process unconditionally.
#
# What it does:
#
#   Patch A — _patch_claude_code_realtime_hooks()
#     Wraps ClaudeCode.install() to install opik/uuid6/socksio inside the
#     Docker container (using whatever package manager is available), and wraps
#     ClaudeCode.run() to inject a Claude Code settings.json that registers a
#     hook command for every supported lifecycle event
#     (UserPromptSubmit, PostToolUse, PostToolUseFailure, PreCompact, Stop,
#      SubagentStart, SubagentStop, SessionEnd).
#     Each hook fires: python3 <hook_path> <event>
#     which streams a span to the Opik ingestion API in real time.
#
#   Patch B — _patch_claude_code_fallback()
#     Wraps ClaudeCode.populate_context_post_run().  If trajectory.json is
#     missing after a run (i.e. realtime hooks failed), this patch reads the
#     raw claude-code.txt event stream, converts it to a trajectory via
#     Harbor's _convert_events_to_trajectory(), writes trajectory.json, and
#     backfills token/cost metrics on the evaluation context.
#
# Both patches guard against double-application and gracefully no-op when the
# Harbor agent module cannot be imported (e.g. wrong package version).

import json
import os
import shlex
import sys
from pathlib import Path
from types import MethodType

HARBOR_RUNTIME_DIR = (
    Path(__file__).resolve().parents[1] / "utils" / "common" / "Harbor"
)
sys.path.append(str(HARBOR_RUNTIME_DIR))

from opik_trace_gate import _is_true, opik_tracing_enabled

_HOOK_EVENTS = [
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
]


def _rust_package_mirror_bootstrap(extra_env: dict[str, str] | None) -> str:
    extra_env = extra_env or {}
    rustup_update_root = extra_env.get("RUSTUP_UPDATE_ROOT", "")
    rustup_dist_server = extra_env.get("RUSTUP_DIST_SERVER", "")
    cargo_replace_with = extra_env.get("CARGO_REGISTRY_REPLACE_WITH", "")
    cargo_registry_url = extra_env.get("CARGO_REGISTRY_URL", "")

    parts = []
    if rustup_update_root:
        parts.append(f"export RUSTUP_UPDATE_ROOT={shlex.quote(rustup_update_root)}")
    if rustup_dist_server:
        parts.append(f"export RUSTUP_DIST_SERVER={shlex.quote(rustup_dist_server)}")
    if cargo_replace_with and cargo_registry_url:
        parts.extend(
            [
                f"export CARGO_REGISTRY_REPLACE_WITH={shlex.quote(cargo_replace_with)}",
                f"export CARGO_REGISTRY_URL={shlex.quote(cargo_registry_url)}",
                "cargo_home=\"${CARGO_HOME:-$HOME/.cargo}\"",
                "mkdir -p \"$cargo_home\"",
                "cargo_config=\"$cargo_home/config.toml\"",
                "tmp_config=\"$cargo_home/config.toml.tmp.$$\"",
                (
                    "if [ -f \"$cargo_config\" ] && command -v awk >/dev/null 2>&1; then "
                    "awk -v mirror=\"$CARGO_REGISTRY_REPLACE_WITH\" "
                    "'BEGIN { skip=0 } "
                    "/^\\[/ { skip = ($0 == \"[source.crates-io]\" || $0 == \"[source.\" mirror \"]\" || $0 == \"[registries.\" mirror \"]\") } "
                    "!skip { print }' \"$cargo_config\" > \"$tmp_config\"; "
                    "elif [ -f \"$cargo_config\" ]; then cp \"$cargo_config\" \"$tmp_config\"; "
                    "else : > \"$tmp_config\"; fi"
                ),
                (
                    "printf '\\n[source.crates-io]\\nreplace-with = \"%s\"\\n\\n[source.%s]\\nregistry = \"%s\"\\n\\n[registries.%s]\\nindex = \"%s\"\\n' "
                    "\"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_URL\" "
                    "\"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_URL\" >> \"$tmp_config\""
                ),
                "mv \"$tmp_config\" \"$cargo_config\"",
            ]
        )

    if not parts:
        return ""
    return "set +e; " + "; ".join(parts) + "; set -e"


def _fix_unquoted_append_system_prompt(command: str) -> str:
    """Fix Harbor's missing shell-quoting of --append-system-prompt value.

    Harbor concatenates the claude CLI command as a plain string without
    shell-quoting the --append-system-prompt value.  When bash executes
    the string, it splits the value on spaces, turning it into stray
    positional arguments.  Claude Code's CLI parser then consumes the
    first word as the value and treats the rest as the user message,
    so the actual task instruction is never delivered to the model.

    This function detects an unquoted value and wraps it in single quotes
    so bash passes the full string as a single argument.
    """
    import re as _re

    if "--append-system-prompt" not in command:
        return command
    # Match --append-system-prompt VALUE where VALUE is not already quoted.
    # The value runs until the next --flag (e.g. --disallowedTools, --print).
    m = _re.search(
        r"(--append-system-prompt\s+)([^'\"\s]\S.*?)(\s+--[a-zA-Z])",
        command,
        _re.DOTALL,
    )
    if not m:
        return command
    value = m.group(2)
    return command[: m.start(2)] + shlex.quote(value) + command[m.end(2):]


def _hook_enabled(extra_env: dict[str, str] | None) -> bool:
    if not extra_env:
        return False
    if not opik_tracing_enabled(extra_env):
        return False
    return _is_true(extra_env.get("CC_OPIK_ENABLE_HOOK", "true"))


def _hook_mount_path(extra_env: dict[str, str] | None) -> str:
    if not extra_env:
        return "/opt/tb-opik/claude_realtime_trace.py"
    return extra_env.get(
        "CC_OPIK_HOOK_MOUNT_PATH", "/opt/tb-opik/claude_realtime_trace.py"
    )


def _build_web_mcp_config(server_path: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "web": {
                    "type": "stdio",
                    "command": "python3",
                    "args": [server_path],
                }
            }
        },
        ensure_ascii=True,
    )


def _build_hook_settings_json(hook_path: str) -> str:
    def hook_command(event: str, extra_args: str = "") -> str:
        return (
        "for py in /opt/python3.12-runtime/bin/python3.12 python3.12 python3; do "
        "([ -x \"$py\" ] || command -v \"$py\" >/dev/null 2>&1) || continue; "
        "\"$py\" - <<'PY' >/dev/null 2>&1 || continue\n"
        "import opik, uuid6, socksio\n"
        "PY\n"
            f"exec \"$py\" {shlex.quote(hook_path)} {event}{extra_args}; "
        "done; "
            f"exec python3 {shlex.quote(hook_path)} {event}{extra_args}"
        )

    def event_command(event: str) -> str:
        command = hook_command(event)
        if event != "SessionEnd":
            # Do not start a login shell here. Some managed Sandbox images
            # ship Bash-only snippets in /etc/profile.d, while /bin/sh is
            # dash. ``sh -l`` sources those snippets and aborts the hook before
            # Claude can make its first model call.
            return "sh -c " + shlex.quote(command)

        # Claude Code often cancels SessionEnd hooks while shutting down. Persist
        # stdin first, then finalize from a detached child so completed traces do
        # not remain in Opik as running.
        detached = (
            'payload="${TMPDIR:-/tmp}/cc-opik-sessionend-$(date +%s%N)-$$.json"; '
            'cat > "$payload"; '
            "nohup sh -c "
            + shlex.quote(hook_command("SessionEnd", ' --payload-file "$1"'))
            + ' _ "$payload" >/dev/null 2>&1 &'
        )
        return "sh -c " + shlex.quote(detached)

    payload = {
        "alwaysThinkingEnabled": True,
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            # Use the injected Python runtime when present. The
                            # task image's python3 may be 3.13 while our offline
                            # Opik wheels are built for Python 3.12.
                            "command": event_command(event),
                        }
                    ]
                }
            ]
            for event in _HOOK_EVENTS
        },
    }
    return json.dumps(payload, ensure_ascii=True)


def _patch_claude_code_realtime_hooks() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:  # noqa: BLE001 - Harbor is optional at interpreter startup
        return

    if getattr(ClaudeCode, "_opik_realtime_hooks_patch_applied", False):
        return

    original_install = ClaudeCode.install
    original_run = ClaudeCode.run

    async def patched_install(self, environment):  # type: ignore[no-untyped-def]
        extra_env = getattr(self, "_extra_env", None)
        npm_registry = (extra_env or {}).get("NPM_CONFIG_REGISTRY", "")
        opensandbox_runtime = (
            os.environ.get("HARBOR_ENVIRONMENT_TYPE", "").strip().lower()
            == "opensandbox"
        )
        local_claude_tgz = (extra_env or {}).get(
            "CC_OPIK_CLAUDE_TGZ_PATH", "/opt/tb-opik/claude-code.tgz"
        )
        local_node_runtime = (
            (extra_env or {}).get(
                "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
            )
            + "/node-runtime.tar.xz"
        )

        # Intercept both exec_as_root and exec_as_agent during install so that:
        #   exec_as_root: keep the task image's apt sources intact and only
        #                 force IPv4. Rewriting http apt mirrors to https breaks
        #                 some task containers that do not have trusted CA roots.
        #   exec_as_agent: replace the bootstrap script with the configured npm
        #                  registry path. This works with mounted caches and
        #                  managed Sandboxes without requiring one particular
        #                  public installer endpoint.
        import re as _re

        def _make_claude_install_command(command: str) -> str:
            m = _re.search(r"@anthropic-ai/claude-code@([\d][^\s'\";]*)", command)
            if not m:
                # Harbor's bootstrap.sh install passes the version as
                # "bash -s -- 2.1.90" rather than in the npm spec format.
                m = _re.search(r"bash -s --\s+([\d][^\s'\";]*)", command)
            version_suffix = f"@{m.group(1)}" if m else ""
            npm_prefix = (
                f"NPM_CONFIG_REGISTRY={shlex.quote(npm_registry)} "
                if npm_registry
                else ""
            )
            claude_tgz_path = shlex.quote(local_claude_tgz)
            claude_tgz_url = shlex.quote((extra_env or {}).get("HARBOR_LOCAL_CLAUDE_TGZ_URL", ""))
            node_runtime_path = shlex.quote(local_node_runtime)
            npm_cache_path = shlex.quote(
                (extra_env or {}).get("CC_OPIK_NPM_CACHE_DIR", "")
                or (extra_env or {}).get("CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels")
                + "/npm-cache"
            )
            node_dist_url = shlex.quote((extra_env or {}).get("CC_NODE_DIST_URL", ""))
            node_runtime_condition = (
                f"[ -f {node_runtime_path} ]"
                if opensandbox_runtime
                else (
                    "! command -v npm >/dev/null 2>&1 && "
                    f"[ -f {node_runtime_path} ] && "
                    "command -v python3 >/dev/null 2>&1"
                )
            )
            node_fallback_condition = (
                "node_runtime_required"
                if opensandbox_runtime
                else "! command -v npm >/dev/null 2>&1"
            )
            return (
                "set -euo pipefail; "
                f"if [ ! -f {claude_tgz_path} ] && [ -n {claude_tgz_url} ]; then "
                "  tmp_tgz=\"$(mktemp /tmp/claude-code-XXXXXX.tgz)\"; "
                f"  python3 - <<'PY' {claude_tgz_url} \"$tmp_tgz\" >/dev/null 2>&1 || true\n"
                "import sys, urllib.request\n"
                "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])\n"
                "PY\n"
                "  if [ -s \"$tmp_tgz\" ]; then claude_tgz_path=\"$tmp_tgz\"; else claude_tgz_path=\"\"; fi; "
                "else "
                f"  claude_tgz_path={claude_tgz_path}; "
                "fi; "
                "node_runtime_required() { "
                "  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then "
                "    return 0; "
                "  fi; "
                "  ! node -e 'process.exit(Number(process.versions.node.split(\".\")[0]) >= 18 ? 0 : 1)' "
                "    >/dev/null 2>&1; "
                "}; "
                # Prefer the offline Node runtime prepared by monitor_harbor.sh.
                # Task images may lack npm or carry a Node release too old for
                # Claude Code. In both cases the S3-materialized runtime is the
                # authoritative Node environment; apt is only a final fallback.
                f"if {node_runtime_condition}; then "
                "  node_dir=\"$(mktemp -d /tmp/tb-node-XXXXXX)\"; "
                "  node_extract_ok=0; "
                "  if command -v python3 >/dev/null 2>&1; then "
                "    if python3 - <<'PY' "
                f"{node_runtime_path}"
                " \"$node_dir\"\n"
                "import sys, tarfile\n"
                "with tarfile.open(sys.argv[1]) as archive:\n"
                "    archive.extractall(sys.argv[2])\n"
                "PY\n"
                "    then node_extract_ok=1; fi; "
                "  elif command -v tar >/dev/null 2>&1 && "
                f"tar -xJf {node_runtime_path} -C \"$node_dir\"; then "
                "    node_extract_ok=1; "
                "  fi; "
                "  node_bin=\"\"; "
                "  if [ \"$node_extract_ok\" = 1 ]; then "
                "    node_bin=\"$(find \"$node_dir\" -type f -path '*/bin/node' -print -quit 2>/dev/null)\"; "
                "  fi; "
                "  if [ -n \"$node_bin\" ] && "
                "\"$node_bin\" -e 'process.exit(Number(process.versions.node.split(\".\")[0]) >= 18 ? 0 : 1)' "
                "    >/dev/null 2>&1; then "
                "    node_runtime_bin=\"$(dirname \"$node_bin\")\"; "
                "    mkdir -p \"$HOME/.local/bin\"; "
                "    ln -sf \"$node_runtime_bin/node\" \"$HOME/.local/bin/node\" 2>/dev/null || true; "
                "    ln -sf \"$node_runtime_bin/npm\" \"$HOME/.local/bin/npm\" 2>/dev/null || true; "
                "    ln -sf \"$node_runtime_bin/npx\" \"$HOME/.local/bin/npx\" 2>/dev/null || true; "
                "    export PATH=\"$node_runtime_bin:$HOME/.local/bin:$PATH\"; "
                "    hash -r 2>/dev/null || true; "
                "    echo \"Using S3 Node runtime: $(node --version)\" >&2; "
                "  fi; "
                "fi; "
                # Sandboxes without host mounts can get Node from an explicitly
                # configured, Sandbox-reachable dist endpoint instead of
                # depending on the task image's package manager.
                f"if {node_fallback_condition} && [ -n "
                f"{node_dist_url}"
                " ] && command -v python3 >/dev/null 2>&1; then "
                "  node_dist_tgz=\"$(mktemp /tmp/tb-node-dist-XXXXXX.tgz)\"; "
                f"  python3 - <<'PY' {node_dist_url} \"$node_dist_tgz\" || true\n"
                "import shutil, sys, urllib.request\n"
                "with urllib.request.urlopen(sys.argv[1], timeout=180) as r, open(sys.argv[2], 'wb') as f:\n"
                "    shutil.copyfileobj(r, f)\n"
                "PY\n"
                "  if [ -s \"$node_dist_tgz\" ]; then "
                "    node_dir=\"$(mktemp -d /tmp/tb-node-XXXXXX)\"; "
                "    if python3 - <<'PY' \"$node_dist_tgz\" \"$node_dir\"\n"
                "import sys, tarfile\n"
                "with tarfile.open(sys.argv[1]) as archive:\n"
                "    archive.extractall(sys.argv[2])\n"
                "PY\n"
                "    then "
                "      node_bin=\"$(find \"$node_dir\" -type f -path '*/bin/node' -print -quit 2>/dev/null)\"; "
                "      if [ -n \"$node_bin\" ]; then "
                "        node_runtime_bin=\"$(dirname \"$node_bin\")\"; "
                "        mkdir -p \"$HOME/.local/bin\"; "
                "        ln -sf \"$node_runtime_bin/node\" \"$HOME/.local/bin/node\" 2>/dev/null || true; "
                "        ln -sf \"$node_runtime_bin/npm\" \"$HOME/.local/bin/npm\" 2>/dev/null || true; "
                "        ln -sf \"$node_runtime_bin/npx\" \"$HOME/.local/bin/npx\" 2>/dev/null || true; "
                "        export PATH=\"$HOME/.local/bin:$node_runtime_bin:$PATH\"; "
                "        hash -r 2>/dev/null || true; "
                "      fi; "
                "    fi; "
                "  fi; "
                "fi; "
                "export PATH=\"$HOME/.local/bin:$PATH\"; "
                "claude_check() { "
                "  hash -r 2>/dev/null || true; "
                "  for attempt in 1 2 3; do "
                "    if [ -x \"$HOME/.local/bin/claude\" ]; then "
                "      \"$HOME/.local/bin/claude\" --version && return 0; "
                "    elif command -v claude >/dev/null 2>&1; then "
                "      claude --version && return 0; "
                "    fi; "
                "    sleep 1; "
                "  done; "
                "  return 1; "
                "}; "
                f"if {node_fallback_condition}; then "
                "  if command -v apk >/dev/null 2>&1; then "
                "    apk add --no-cache nodejs npm bash curl; "
                "  elif command -v apt-get >/dev/null 2>&1; then "
                "    apt-get -o Acquire::ForceIPv4=true update -qq && "
                "    apt-get install -y -qq nodejs npm; "
                "  elif command -v yum >/dev/null 2>&1; then "
                "    yum install -y nodejs npm; "
                "  fi; "
                "fi; "
                "mkdir -p \"$HOME/.local/bin\"; "
                "if command -v npm >/dev/null 2>&1; then npm config set prefix \"$HOME/.local\" >/dev/null 2>&1 || true; fi; "
                "claude_installed=0; "
                "if command -v npm >/dev/null 2>&1 && [ -n \"${claude_tgz_path:-}\" ]; then "
                f"  if [ -d {npm_cache_path} ]; then "
                "    npm_cache_tmp=\"$(mktemp -d /tmp/tb-npm-cache-XXXXXX)\"; "
                # The shared cache is mounted read-only into task containers, but
                # npm still writes tmp/index metadata even for --offline installs.
                f"    if cp -a {npm_cache_path}/. \"$npm_cache_tmp\"/ && "
                "npm install -g --offline --cache \"$npm_cache_tmp\" \"${claude_tgz_path}\" && claude_check; then "
                "      claude_installed=1; "
                "    fi; "
                "  fi; "
                "  if [ \"$claude_installed\" != 1 ] && "
                "npm install -g \"${claude_tgz_path}\" && claude_check; then "
                "    claude_installed=1; "
                "  fi; "
                "fi; "
                "if [ \"$claude_installed\" != 1 ]; then "
                f"  {npm_prefix}npm install -g @anthropic-ai/claude-code{version_suffix} && "
                "  echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
                "  export PATH=\"$HOME/.local/bin:$PATH\" && "
                "  claude_check; "
                "fi"
            )

        _apt_fix = (
            "{ "
            "echo 'Acquire::ForceIPv4 \"true\";' "
            "> /etc/apt/apt.conf.d/99force-ipv4; "
            "} 2>/dev/null || true; "
        )

        original_exec_as_root = self.exec_as_root
        original_exec_as_agent = self.exec_as_agent

        async def _exec_as_root_install_fix(
            _self, environment, command=None, env=None, cwd=None, timeout_sec=None,
        ):
            if command and "apt-get" in command:
                original_command = f"set -euo pipefail; {_apt_fix}{command}"
                if opensandbox_runtime:
                    local_runtime_guard = (
                        f"[ -f {shlex.quote(local_claude_tgz)} ] && "
                        f"[ -f {shlex.quote(local_node_runtime)} ]"
                    )
                    command = (
                        f"if {local_runtime_guard}; then "
                        "if command -v bash >/dev/null 2>&1 && "
                        "command -v ps >/dev/null 2>&1 && "
                        "command -v pgrep >/dev/null 2>&1; then "
                        "echo 'OpenSandbox local Claude runtime and system prerequisites ready; skip package bootstrap' >&2; "
                        "elif command -v apk >/dev/null 2>&1; then "
                        "apk add --no-cache bash procps; "
                        "elif command -v apt-get >/dev/null 2>&1; then "
                        f"{_apt_fix}apt-get update && apt-get install -y bash procps; "
                        "elif command -v yum >/dev/null 2>&1; then "
                        "yum install -y bash procps-ng; "
                        "else "
                        "echo 'OpenSandbox local Claude runtime requires bash, ps, and pgrep' >&2; "
                        "exit 1; "
                        "fi; "
                        f"else {original_command}; fi"
                    )
                else:
                    command = original_command
            return await original_exec_as_root(
                environment, command=command, env=env, cwd=cwd, timeout_sec=timeout_sec,
            )

        async def _exec_as_agent_install_fix(
            _self, environment, command=None, env=None, cwd=None, timeout_sec=None,
        ):
            if command and ("claude.ai/install.sh" in command or "downloads.claude.ai/claude-code-releases/bootstrap.sh" in command):
                command = _make_claude_install_command(command)
            return await original_exec_as_agent(
                environment, command=command, env=env, cwd=cwd, timeout_sec=timeout_sec,
            )

        self.exec_as_root = MethodType(_exec_as_root_install_fix, self)
        self.exec_as_agent = MethodType(_exec_as_agent_install_fix, self)
        try:
            await original_install(self, environment)
        finally:
            self.exec_as_root = original_exec_as_root
            self.exec_as_agent = original_exec_as_agent

        extra_env = getattr(self, "_extra_env", None)
        rust_mirror_bootstrap = _rust_package_mirror_bootstrap(extra_env)
        if rust_mirror_bootstrap:
            await self.exec_as_agent(environment, command=rust_mirror_bootstrap)

        if not _hook_enabled(extra_env):
            return
        if not _is_true((extra_env or {}).get("CC_OPIK_INSTALL_DEPS", "true")):
            return

        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                f"wheel_dir={shlex.quote((extra_env or {}).get('CC_OPIK_PY_WHEEL_DIR', '/opt/tb-opik/python-wheels'))}; "
                "if ! command -v python3 >/dev/null 2>&1; then "
                "if command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache python3 py3-pip; "
                "elif command -v apt-get >/dev/null 2>&1; then "
                "apt-get update && apt-get install -y python3 python3-pip; "
                "elif command -v yum >/dev/null 2>&1; then "
                "yum install -y python3 python3-pip; "
                "else "
                "echo '[WARN] no known package manager, skip python dependency install' >&2; "
                "fi; "
                "fi; "
                "if ! command -v python3.12 >/dev/null 2>&1 "
                "&& [ -f \"$wheel_dir/python3.12-runtime.tar.gz\" ] "
                "&& command -v python3 >/dev/null 2>&1; then "
                "rm -rf /opt/python3.12-runtime; "
                "mkdir -p /opt; "
                "python3 - <<'PY' \"$wheel_dir/python3.12-runtime.tar.gz\" /opt\n"
                "import sys, tarfile\n"
                "with tarfile.open(sys.argv[1], 'r:gz') as archive:\n"
                "    archive.extractall(sys.argv[2])\n"
                "PY\n"
                "fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "py_bin=\"$(command -v python3.12 || true)\"; "
                "py_bin=\"\"; "
                "for candidate in /opt/python3.12-runtime/bin/python3.12 python3.12 python3; do "
                "([ -x \"$candidate\" ] || command -v \"$candidate\" >/dev/null 2>&1) || continue; "
                "\"$candidate\" - <<'PY' >/dev/null 2>&1 || continue\n"
                "import sys\n"
                "print(sys.version)\n"
                "PY\n"
                "py_bin=\"$candidate\"; "
                "break; "
                "done; "
                "if [ -z \"$py_bin\" ]; then "
                "echo '[WARN] python missing, skip opik hook deps' >&2; "
                "exit 0; "
                "fi; "
                f"wheel_dir={shlex.quote((extra_env or {}).get('CC_OPIK_PY_WHEEL_DIR', '/opt/tb-opik/python-wheels'))}; "
                f"wheel_url={shlex.quote((extra_env or {}).get('HARBOR_LOCAL_WHEEL_SERVER_URL', ''))}; "
                "missing=$(\"$py_bin\" - <<'PY'\n"
                "import importlib.util\n"
                "mods = ('opik', 'uuid6', 'socksio')\n"
                "missing = [m for m in mods if importlib.util.find_spec(m) is None]\n"
                "print(' '.join(missing))\n"
                "PY\n"
                "); "
                "if [ -z \"$missing\" ]; then exit 0; fi; "
                "pip_opts=\"\"; "
                "if [ -d \"$wheel_dir\" ]; then "
                "pip_opts=\"--no-index --find-links $wheel_dir\"; "
                "elif [ -n \"$wheel_url\" ]; then "
                "trusted_host=\"$(printf %s \"$wheel_url\" | sed -E 's#^https?://([^/:]+).*#\\1#')\"; "
                "pip_opts=\"--trusted-host $trusted_host --no-index --find-links $wheel_url\"; "
                "fi; "
                "if ! \"$py_bin\" -m pip --version >/dev/null 2>&1; then "
                "if [ -f \"$wheel_dir/get-pip.py\" ]; then "
                "\"$py_bin\" \"$wheel_dir/get-pip.py\" --break-system-packages $pip_opts pip setuptools wheel >/dev/null 2>&1 || true; "
                "elif [ -n \"$wheel_url\" ]; then "
                "tmp_get_pip=\"$(mktemp /tmp/get-pip-XXXXXX.py)\"; "
                "\"$py_bin\" - <<'PY' \"$wheel_url/get-pip.py\" \"$tmp_get_pip\" >/dev/null 2>&1 || true\n"
                "import sys, urllib.request\n"
                "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])\n"
                "PY\n"
                "if [ -s \"$tmp_get_pip\" ]; then \"$py_bin\" \"$tmp_get_pip\" --break-system-packages $pip_opts pip setuptools wheel >/dev/null 2>&1 || true; fi; "
                "rm -f \"$tmp_get_pip\"; "
                "fi; "
                "fi; "
                "\"$py_bin\" -m pip install --break-system-packages --ignore-installed $pip_opts $missing "
                "|| \"$py_bin\" -m pip install --ignore-installed $pip_opts $missing "
                "|| \"$py_bin\" -m pip install --user --ignore-installed $pip_opts $missing "
                "|| \"$py_bin\" -m pip install --break-system-packages --ignore-installed $missing "
                "|| \"$py_bin\" -m pip install --user --ignore-installed $missing "
                "|| { echo '[WARN] failed to install python deps for opik hook' >&2; exit 1; }"
            ),
        )

    async def patched_run(self, instruction, environment, context):  # type: ignore[no-untyped-def]
        extra_env = getattr(self, "_extra_env", None)
        hook_enabled = _hook_enabled(extra_env)

        original_exec_as_agent = self.exec_as_agent

        async def exec_as_agent_with_hook(
            _self,
            environment,
            command,
            env=None,
            cwd=None,
            timeout_sec=None,
        ):
            # This compatibility fix is independent of Opik and must always
            # run, including when external tracing is disabled.
            patched_command = _fix_unquoted_append_system_prompt(command)
            if "claude --verbose --output-format=stream-json" in patched_command:
                web_mcp_path = (extra_env or {}).get("CC_WEB_MCP_PATH", "").strip()
                prefix = 'export PATH="$HOME/.local/bin:$PATH"; '
                if web_mcp_path:
                    config_json = shlex.quote(_build_web_mcp_config(web_mcp_path))
                    config_path = "$HOME/.claude/web-mcp.json"
                    prefix = (
                        "mkdir -p \"$HOME/.claude\"; "
                        f"printf '%s\\n' {config_json} > \"{config_path}\"; "
                        + prefix
                    )
                    patched_command = patched_command.replace(
                        "claude --verbose",
                        "claude --strict-mcp-config "
                        f"--mcp-config \"{config_path}\" "
                        "--allowedTools mcp__web__web_search,mcp__web__web_fetch "
                        "--verbose",
                        1,
                    )
                patched_command = f"{prefix}{patched_command}"
                if hook_enabled:
                    # Harbor assigns this after constructing the agent, so it
                    # cannot be supplied by the launcher as static --ae data.
                    # Pass the exact trial identity to Claude so every hook
                    # subprocess can correlate its Agent trace with the Harbor
                    # trial trace.
                    env = dict(env or {})
                    agent_session_id = str(
                        getattr(_self, "session_id", "") or ""
                    )
                    trial_suffix = "__agent"
                    if agent_session_id.endswith(trial_suffix):
                        env.setdefault(
                            "HARBOR_TRIAL_ID",
                            agent_session_id[: -len(trial_suffix)],
                        )
            if not hook_enabled:
                return await original_exec_as_agent(
                    environment,
                    patched_command,
                    env=env,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                )

            # Harbor AK 2.1.69 run() first calls exec_as_agent with a setup
            # command that creates $CLAUDE_CONFIG_DIR subdirectories (debug,
            # projects/-app, etc.) but never writes settings.json.  We detect
            # this setup command by its unique "CLAUDE_CONFIG_DIR/debug" marker
            # and append a printf that writes our hook configuration into
            # $CLAUDE_CONFIG_DIR/settings.json so Claude Code picks it up.
            if (
                hook_enabled
                and "CLAUDE_CONFIG_DIR/debug" in command
                and "mkdir -p" in command
                and not getattr(_self, "_opik_hook_settings_written", False)
            ):
                settings_json = shlex.quote(
                    _build_hook_settings_json(_hook_mount_path(extra_env))
                )
                patched_command = (
                    f"{patched_command} && "
                    "mkdir -p $HOME/.claude && "
                    f"printf '%s\n' {settings_json} > $CLAUDE_CONFIG_DIR/settings.json && "
                    f"printf '%s\n' {settings_json} > $HOME/.claude/settings.json"
                )
                _self._opik_hook_settings_written = True

            return await original_exec_as_agent(
                environment,
                patched_command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        self.exec_as_agent = MethodType(exec_as_agent_with_hook, self)
        try:
            return await original_run(self, instruction, environment, context)
        finally:
            self.exec_as_agent = original_exec_as_agent

    ClaudeCode.install = patched_install
    ClaudeCode.run = patched_run
    ClaudeCode._opik_realtime_hooks_patch_applied = True


def _patch_claude_code_fallback() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:  # noqa: BLE001 - Harbor is optional at interpreter startup
        return

    if getattr(ClaudeCode, "_opik_fallback_patch_applied", False):
        return

    original_populate = ClaudeCode.populate_context_post_run

    def _build_fallback_session_dir(logs_dir: Path) -> Path | None:
        stream_log = logs_dir / "claude-code.txt"
        if not stream_log.is_file():
            return None

        session_dir = logs_dir / "_opik_fallback_session"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "fallback.jsonl"

        event_count = 0
        with (
            open(stream_log, "r", encoding="utf-8", errors="replace") as src,
            open(session_file, "w", encoding="utf-8") as dst,
        ):
            for raw_line in src:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                dst.write(json.dumps(event, ensure_ascii=False))
                dst.write("\n")
                event_count += 1

        if event_count == 0:
            return None

        return session_dir

    def patched_populate(self, context):  # type: ignore[no-untyped-def]
        original_populate(self, context)

        trajectory_path = self.logs_dir / "trajectory.json"
        if trajectory_path.exists():
            return

        fallback_dir = _build_fallback_session_dir(self.logs_dir)
        if fallback_dir is None:
            return

        try:
            trajectory = self._convert_events_to_trajectory(fallback_dir)
        except Exception:  # noqa: BLE001 - fallback conversion is best effort
            return

        if not trajectory:
            return

        try:
            with open(trajectory_path, "w", encoding="utf-8") as handle:
                json.dump(
                    trajectory.to_json_dict(),
                    handle,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError:
            return

        if trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.cost_usd = metrics.total_cost_usd
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0

    ClaudeCode.populate_context_post_run = patched_populate
    ClaudeCode._opik_fallback_patch_applied = True


from e2b_runtime import patch_e2b_runtime_from_env

patch_e2b_runtime_from_env()
if (
    os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower() == "qz"
    and os.environ.get("QZ_SANDBOX_TEMPLATE_MAP", "").strip()
):
    from qz_task_instruction import patch_harbor_task_instruction

    patch_harbor_task_instruction()
_patch_claude_code_realtime_hooks()
_patch_claude_code_fallback()
