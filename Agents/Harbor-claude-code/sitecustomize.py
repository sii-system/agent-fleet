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

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import tempfile
import uuid
from pathlib import Path
from types import MethodType


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
_ROUND_GATE_EVENTS = ("PreToolUse", "Stop")
_MISSING = object()


def _is_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
                "if [ -f \"$cargo_config\" ] && command -v awk >/dev/null 2>&1; then "
                "awk -v mirror=\"$CARGO_REGISTRY_REPLACE_WITH\" "
                "'BEGIN { skip=0 } "
                "/^\\[/ { skip = ($0 == \"[source.crates-io]\" || $0 == \"[source.\" mirror \"]\" || $0 == \"[registries.\" mirror \"]\") } "
                "!skip { print }' \"$cargo_config\" > \"$tmp_config\"; "
                "elif [ -f \"$cargo_config\" ]; then cp \"$cargo_config\" \"$tmp_config\"; "
                "else : > \"$tmp_config\"; fi",
                "printf '\\n[source.crates-io]\\nreplace-with = \"%s\"\\n\\n[source.%s]\\nregistry = \"%s\"\\n\\n[registries.%s]\\nindex = \"%s\"\\n' "
                "\"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_URL\" "
                "\"$CARGO_REGISTRY_REPLACE_WITH\" \"$CARGO_REGISTRY_URL\" >> \"$tmp_config\"",
                "mv \"$tmp_config\" \"$cargo_config\"",
            ]
        )

    if not parts:
        return ""
    return "set +e; " + "; ".join(parts) + "; set -e"


def _write_local_append_system_prompt(prompt: str) -> tuple[Path, int, str]:
    """Write exact prompt bytes to a private host-side staging file."""
    payload = prompt.encode("utf-8")
    fd, raw_path = tempfile.mkstemp(
        prefix="harbor-claude-append-system-prompt-", suffix=".txt"
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path, len(payload), hashlib.sha256(payload).hexdigest()


async def _install_remote_append_system_prompt(
    agent, environment, local_path: Path, remote_path: str
) -> None:
    """Upload the prompt and prove that Claude's runtime user can read it."""
    identity = await agent.exec_as_agent(
        environment,
        command='printf "%s %s\\n" "$(id -u)" "$(id -g)"',
        timeout_sec=30,
    )
    identity_parts = (identity.stdout or "").strip().split()
    if len(identity_parts) != 2 or not all(part.isdigit() for part in identity_parts):
        raise RuntimeError(
            "Unable to resolve Claude runtime UID/GID for append-system-prompt-file"
        )
    uid, gid = identity_parts

    await environment.upload_file(local_path, remote_path)
    quoted_path = shlex.quote(remote_path)
    await agent.exec_as_root(
        environment,
        command=f"chown {uid}:{gid} {quoted_path} && chmod 600 {quoted_path}",
        timeout_sec=30,
    )
    await agent.exec_as_agent(
        environment,
        command=f"test -f {quoted_path} && test -r {quoted_path}",
        timeout_sec=30,
    )


async def _remove_remote_append_system_prompt(
    environment, remote_path: str, logger
) -> None:
    """Best-effort cleanup that never masks the trial's primary outcome."""
    try:
        result = await environment.exec(
            command=f"rm -f -- {shlex.quote(remote_path)}",
            user="root",
            timeout_sec=30,
        )
        if result.return_code != 0:
            logger.warning(
                "Failed to remove remote append-system-prompt file %s (exit %s)",
                remote_path,
                result.return_code,
            )
    except Exception as exc:
        logger.warning(
            "Failed to remove remote append-system-prompt file %s: %s",
            remote_path,
            exc,
        )


def _inject_append_system_prompt_file(command: str, remote_path: str) -> str:
    """Insert a file-backed system prompt before Claude's print delimiter."""
    marker = " --print --"
    marker_pos = command.find(marker)
    if marker_pos < 0:
        raise RuntimeError(
            "Claude command is missing the --print -- insertion point for "
            "append-system-prompt-file"
        )
    if " --append-system-prompt " in command:
        raise RuntimeError(
            "Unsafe inline --append-system-prompt remained in the Claude command"
        )
    if " --append-system-prompt-file " in command:
        raise RuntimeError("Duplicate --append-system-prompt-file in Claude command")
    file_arg = f" --append-system-prompt-file {shlex.quote(remote_path)}"
    return command[:marker_pos] + file_arg + command[marker_pos:]


def _replace_print_instruction(command: str, instruction: str) -> str:
    """Preserve the main instruction across Harbor's nested shell boundary."""
    marker = " --print -- "
    tail_marker = " 2>&1 </dev/null | tee "
    start = command.find(marker)
    if start < 0:
        return command
    value_start = start + len(marker)
    end = command.find(tail_marker, value_start)
    if end < 0:
        return command
    encoded = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
    safe_value = f'"$(printf %s {shlex.quote(encoded)} | base64 -d)"'
    return command[:value_start] + safe_value + command[end:]


def _claude_stream_command(command: str) -> bool:
    return (
        "claude --verbose --output-format=stream-json" in command
        and " --print --" in command
    )


def _append_claude_exit_diagnostics(command: str) -> str:
    """Persist Claude/tee statuses without changing pipefail semantics."""
    marker = " 2>&1 </dev/null | tee "
    if marker not in command or "claude-wrapper-exit.log" in command:
        return command
    return command + (
        '; __tb_pipeline_status=("${PIPESTATUS[@]}"); '
        '__tb_pipeline_rc=0; '
        'for __tb_status in "${__tb_pipeline_status[@]}"; do '
        'if [ "$__tb_status" -ne 0 ]; then __tb_pipeline_rc="$__tb_status"; fi; '
        'done; '
        'printf "%s\\n" '
        '"time=$(date -Is) pipeline=${__tb_pipeline_status[*]} return=$__tb_pipeline_rc" '
        '>> /logs/agent/claude-wrapper-exit.log 2>&1 || true; '
        'exit "$__tb_pipeline_rc"'
    )


def _inject_claude_agents_flag(
    command: str, extra_env: dict[str, str] | None
) -> str:
    agents_json = (
        (extra_env or {}).get("TB_CLAUDE_CODE_AGENTS_JSON", "")
        or os.environ.get("TB_CLAUDE_CODE_AGENTS_JSON", "")
    ).strip()
    prefix = "claude --verbose --output-format=stream-json"
    if not agents_json or " --agents " in command or prefix not in command:
        return command
    marker = " --print --"
    addition = f" --agents {shlex.quote(agents_json)}"
    if marker in command:
        return command.replace(marker, addition + marker, 1)
    return command.replace(prefix, prefix + addition, 1)


def _hook_enabled(extra_env: dict[str, str] | None) -> bool:
    if not extra_env:
        return False
    # If CC_OPIK_ENABLE_HOOK is explicitly set, it takes precedence (including
    # explicit false).  Only fall back to TRACE_TO_OPIK when the key is absent.
    if "CC_OPIK_ENABLE_HOOK" in extra_env:
        return _is_true(extra_env["CC_OPIK_ENABLE_HOOK"])
    return _is_true(extra_env.get("TRACE_TO_OPIK"))


def _hook_mount_path(extra_env: dict[str, str] | None) -> str:
    if not extra_env:
        return "/opt/tb-opik/claude_realtime_trace.py"
    return extra_env.get(
        "CC_OPIK_HOOK_MOUNT_PATH", "/opt/tb-opik/claude_realtime_trace.py"
    )


def _round_gate_enabled(extra_env: dict[str, str] | None) -> bool:
    return _is_true(
        (extra_env or {}).get("TB_FUSION_ROUND_GATE")
        or os.environ.get("TB_FUSION_ROUND_GATE")
    )


def _round_gate_path(extra_env: dict[str, str] | None) -> str:
    return (
        (extra_env or {}).get("TB_FUSION_ROUND_GATE_PATH")
        or os.environ.get("TB_FUSION_ROUND_GATE_PATH")
        or "/opt/tb-fusion-round/subagent_barrier_gate.py"
    )


def _round_gate_mode(extra_env: dict[str, str] | None) -> str:
    return (
        (extra_env or {}).get("TB_FUSION_ROUND_GATE_MODE")
        or (extra_env or {}).get("SPAN_FORCE_MODE")
        or os.environ.get("TB_FUSION_ROUND_GATE_MODE")
        or os.environ.get("SPAN_FORCE_MODE")
        or "mid-turn-fusion"
    )


def _build_hook_settings_json(
    hook_path: str,
    *,
    opik_enabled: bool = True,
    round_gate_enabled: bool = False,
    round_gate_path: str = "/opt/tb-fusion-round/subagent_barrier_gate.py",
    round_gate_mode: str = "mid-turn-fusion",
) -> str:
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
            return "sh -lc " + shlex.quote(command)

        # Claude Code often cancels SessionEnd hooks while shutting down. Persist
        # stdin first, then finalize from a detached child so completed traces do
        # not remain in Opik as running.
        detached = (
            'payload="${TMPDIR:-/tmp}/cc-opik-sessionend-$(date +%s%N)-$$.json"; '
            'cat > "$payload"; '
            "nohup sh -lc "
            + shlex.quote(hook_command("SessionEnd", ' --payload-file "$1"'))
            + ' _ "$payload" >/dev/null 2>&1 &'
        )
        return "sh -lc " + shlex.quote(detached)

    hooks = (
        {
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
        }
        if opik_enabled
        else {}
    )
    if round_gate_enabled:
        for event in _ROUND_GATE_EVENTS:
            if opik_enabled and event not in hooks:
                hooks[event] = [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": event_command(event),
                            }
                        ]
                    }
                ]
            hooks.setdefault(event, []).append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f"python3 {shlex.quote(round_gate_path)} "
                                f"{shlex.quote(event)} --mode "
                                f"{shlex.quote(round_gate_mode)}"
                            ),
                        }
                    ]
                }
            )
    payload = {
        "alwaysThinkingEnabled": True,
        "hooks": hooks,
    }
    return json.dumps(payload, ensure_ascii=True)


def _patch_claude_code_realtime_hooks() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:
        return

    if getattr(ClaudeCode, "_opik_realtime_hooks_patch_applied", False):
        return

    original_install = ClaudeCode.install
    original_run = ClaudeCode.run

    async def patched_install(self, environment):  # type: ignore[no-untyped-def]
        extra_env = getattr(self, "_extra_env", None)
        npm_registry = (extra_env or {}).get("NPM_CONFIG_REGISTRY", "")

        # Intercept both exec_as_root and exec_as_agent during install so that:
        #   exec_as_root: keep the task image's apt sources intact and only
        #                 force IPv4. Rewriting http apt mirrors to https breaks
        #                 some task containers that do not have trusted CA roots.
        #   exec_as_agent: the Claude Code install command (curl downloads.claude.ai/.../bootstrap.sh)
        #                  is replaced with npm, because downloads.claude.ai is region-blocked
        #                  on SII servers and returns an HTML page instead of a shell
        #                  script, causing "syntax error near unexpected token '<'".
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
            claude_tgz_path = shlex.quote(
                (extra_env or {}).get("CC_OPIK_CLAUDE_TGZ_PATH", "/opt/tb-opik/claude-code.tgz")
            )
            claude_tgz_url = shlex.quote((extra_env or {}).get("TB_LOCAL_CLAUDE_TGZ_URL", ""))
            node_runtime_path = shlex.quote(
                (extra_env or {}).get("CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels")
                + "/node-runtime.tar.xz"
            )
            npm_cache_path = shlex.quote(
                (extra_env or {}).get("CC_OPIK_NPM_CACHE_DIR", "")
                or (extra_env or {}).get("CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels")
                + "/npm-cache"
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
                # Prefer the offline Node runtime prepared by monitor_harbor.sh.
                # SWE-bench task images often lack npm, and apt may be slow or
                # unavailable inside the isolated task container.
                "if ! command -v npm >/dev/null 2>&1 && [ -f "
                f"{node_runtime_path}"
                " ] && command -v python3 >/dev/null 2>&1; then "
                "  node_dir=\"$(mktemp -d /tmp/tb-node-XXXXXX)\"; "
                "  python3 - <<'PY' "
                f"{node_runtime_path}"
                " \"$node_dir\"\n"
                "import sys, tarfile\n"
                "with tarfile.open(sys.argv[1]) as archive:\n"
                "    archive.extractall(sys.argv[2])\n"
                "PY\n"
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
                "export PATH=\"$HOME/.local/bin:$PATH\"; "
                "if ! command -v npm >/dev/null 2>&1; then "
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
                "if command -v npm >/dev/null 2>&1 && [ -n \"${claude_tgz_path:-}\" ]; then "
                f"  if [ -d {npm_cache_path} ]; then "
                "    npm_cache_tmp=\"$(mktemp -d /tmp/tb-npm-cache-XXXXXX)\"; "
                # The shared cache is mounted read-only into task containers, but
                # npm still writes tmp/index metadata even for --offline installs.
                f"    cp -a {npm_cache_path}/. \"$npm_cache_tmp\"/ && "
                "npm install -g --offline --cache \"$npm_cache_tmp\" \"${claude_tgz_path}\" && claude --version && exit 0; "
                "  fi; "
                "  npm install -g \"${claude_tgz_path}\" && claude --version && exit 0; "
                "fi; "
                f"{npm_prefix}npm install -g @anthropic-ai/claude-code{version_suffix} && "
                "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
                "export PATH=\"$HOME/.local/bin:$PATH\" && "
                "claude --version"
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
                command = f"set -euo pipefail; {_apt_fix}{command}"
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
                f"wheel_url={shlex.quote((extra_env or {}).get('TB_LOCAL_WHEEL_SERVER_URL', ''))}; "
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
        round_gate_enabled = _round_gate_enabled(extra_env)
        agents_enabled = bool(
            (
                (extra_env or {}).get("TB_CLAUDE_CODE_AGENTS_JSON", "")
                or os.environ.get("TB_CLAUDE_CODE_AGENTS_JSON", "")
            ).strip()
        )
        saved_flags = self._resolved_flags
        run_flags = dict(saved_flags)
        append_prompt = run_flags.pop("append_system_prompt", _MISSING)
        prompt_configured = append_prompt is not _MISSING
        if (
            not hook_enabled
            and not round_gate_enabled
            and not agents_enabled
            and not prompt_configured
        ):
            return await original_run(self, instruction, environment, context)

        original_exec_as_agent = self.exec_as_agent
        local_prompt_path: Path | None = None
        remote_prompt_path = ""

        async def exec_as_agent_with_hook(
            _self,
            environment,
            command,
            env=None,
            cwd=None,
            timeout_sec=None,
        ):
            patched_command = command
            if _claude_stream_command(patched_command):
                if remote_prompt_path:
                    patched_command = _inject_append_system_prompt_file(
                        patched_command, remote_prompt_path
                    )
                patched_command = _replace_print_instruction(
                    patched_command, instruction
                )
                patched_command = (
                    "export PATH=\"$HOME/.local/bin:$PATH\"; "
                    f"{patched_command}"
                )
                patched_command = _inject_claude_agents_flag(
                    patched_command, extra_env
                )
            # Harbor AK 2.1.69 run() first calls exec_as_agent with a setup
            # command that creates $CLAUDE_CONFIG_DIR subdirectories (debug,
            # projects/-app, etc.) but never writes settings.json.  We detect
            # this setup command by its unique "CLAUDE_CONFIG_DIR/debug" marker
            # and append a printf that writes our hook configuration into
            # $CLAUDE_CONFIG_DIR/settings.json so Claude Code picks it up.
            if (
                (hook_enabled or round_gate_enabled)
                and "CLAUDE_CONFIG_DIR/debug" in command
                and "mkdir -p" in command
                and not getattr(_self, "_opik_hook_settings_written", False)
            ):
                settings_json = shlex.quote(
                    _build_hook_settings_json(
                        _hook_mount_path(extra_env),
                        opik_enabled=hook_enabled,
                        round_gate_enabled=round_gate_enabled,
                        round_gate_path=_round_gate_path(extra_env),
                        round_gate_mode=_round_gate_mode(extra_env),
                    )
                )
                patched_command = (
                    f"{patched_command} && "
                    "mkdir -p $HOME/.claude && "
                    f"printf '%s\n' {settings_json} > $CLAUDE_CONFIG_DIR/settings.json && "
                    f"printf '%s\n' {settings_json} > $HOME/.claude/settings.json"
                )
                _self._opik_hook_settings_written = True

            if _claude_stream_command(patched_command):
                patched_command = _append_claude_exit_diagnostics(patched_command)

            return await original_exec_as_agent(
                environment,
                patched_command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        self._resolved_flags = run_flags
        try:
            if prompt_configured:
                local_prompt_path, prompt_size, prompt_sha256 = (
                    _write_local_append_system_prompt(str(append_prompt))
                )
                remote_prompt_path = (
                    "/tmp/.harbor-claude-append-system-prompt-"
                    f"{uuid.uuid4().hex}.txt"
                )
                await _install_remote_append_system_prompt(
                    self, environment, local_prompt_path, remote_prompt_path
                )
                self.logger.debug(
                    "Staged append system prompt via file",
                    extra={
                        "prompt_size_bytes": prompt_size,
                        "prompt_sha256": prompt_sha256,
                        "remote_path": remote_prompt_path,
                    },
                )

            self.exec_as_agent = MethodType(exec_as_agent_with_hook, self)
            try:
                return await original_run(self, instruction, environment, context)
            finally:
                self.exec_as_agent = original_exec_as_agent
        finally:
            self._resolved_flags = saved_flags
            if remote_prompt_path:
                await _remove_remote_append_system_prompt(
                    environment, remote_prompt_path, self.logger
                )
            if local_prompt_path is not None:
                try:
                    local_prompt_path.unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "Failed to remove local append-system-prompt file %s: %s",
                        local_prompt_path,
                        exc,
                    )

    ClaudeCode.install = patched_install
    ClaudeCode.run = patched_run
    ClaudeCode._opik_realtime_hooks_patch_applied = True


def _patch_claude_code_fallback() -> None:
    try:
        from harbor.agents.installed.claude_code import ClaudeCode
    except Exception:
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
        with open(stream_log, "r", encoding="utf-8", errors="replace") as src:
            with open(session_file, "w", encoding="utf-8") as dst:
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
        except Exception:
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


_patch_claude_code_realtime_hooks()
_patch_claude_code_fallback()
