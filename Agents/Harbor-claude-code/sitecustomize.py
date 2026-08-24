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

import container_bootstrap  # noqa: E402
from opik_trace_gate import _is_true, opik_tracing_enabled  # noqa: E402

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
            version = m.group(1) if m else ""
            wheel_dir = (extra_env or {}).get(
                "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
            )
            claude_tgz_path = (extra_env or {}).get(
                "CC_OPIK_CLAUDE_TGZ_PATH", "/opt/tb-opik/claude-code.tgz"
            )
            return container_bootstrap.build_npm_tool_install_command(
                container_bootstrap.NpmToolSpec(
                    executable="claude",
                    package="@anthropic-ai/claude-code",
                    version=version,
                    archive_path=claude_tgz_path,
                    archive_url=(extra_env or {}).get(
                        "HARBOR_LOCAL_CLAUDE_TGZ_URL", ""
                    ),
                    archive_basename=Path(claude_tgz_path).name,
                    npm_cache_dir=(extra_env or {}).get("CC_OPIK_NPM_CACHE_DIR", "")
                    or f"{wheel_dir}/npm-cache",
                    npm_registry=npm_registry,
                ),
                wheel_dir=wheel_dir,
                wheel_url=(extra_env or {}).get("HARBOR_LOCAL_WHEEL_SERVER_URL", ""),
                node_dist_url=(extra_env or {}).get("CC_NODE_DIST_URL", ""),
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
            if command and (
                "claude.ai/install.sh" in command
                or "downloads.claude.ai/claude-code-releases/bootstrap.sh" in command
            ):
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

        wheel_dir = (extra_env or {}).get(
            "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
        )
        wheel_url = (extra_env or {}).get("HARBOR_LOCAL_WHEEL_SERVER_URL", "")
        await self.exec_as_root(
            environment,
            command=container_bootstrap.build_python_runtime_command(
                wheel_dir, python_required=False
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(
            environment,
            command=container_bootstrap.build_python_dependencies_command(
                ("opik", "uuid6", "socksio"),
                wheel_dir=wheel_dir,
                wheel_url=wheel_url,
                python_required=False,
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
                patched_command = (
                    "export PATH=\"$HOME/.local/bin:$PATH\"; "
                    f"{patched_command}"
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
