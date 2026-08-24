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
import json
import os
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
HARBOR_RUNTIME_DIR = REPO_ROOT / "Agents" / "utils" / "common" / "Harbor"
sys.path.append(str(HARBOR_RUNTIME_DIR))

import container_bootstrap  # noqa: E402
from opik_trace_gate import opik_tracing_enabled  # noqa: E402

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
TRACE_GATE_PY = HARBOR_RUNTIME_DIR / "opik_trace_gate.py"

CONTAINER_PLUGIN_REL = ".config/opencode/plugins"
CONTAINER_STATE_REL = ".opencode/state"
OPENCODE_RUNTIME_SECRETS_ENV = "OPENCODE_RUNTIME_SECRETS_JSON"


def _load_opencode_runtime_secrets() -> dict[str, str]:
    raw = os.environ.get(OPENCODE_RUNTIME_SECRETS_ENV, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{OPENCODE_RUNTIME_SECRETS_ENV} must be a JSON object"
        ) from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in payload.items()
    ):
        raise ValueError(
            f"{OPENCODE_RUNTIME_SECRETS_ENV} must be a non-empty string map"
        )
    return payload


OPENCODE_RUNTIME_SECRETS = _load_opencode_runtime_secrets()


# ── url helpers ───────────────────────────────────────────────────────────────


def _rewrite_container_proxy(value: str) -> str:
    """Map `127.0.0.1` / `localhost` in a URL netloc to `host.docker.internal`.
    Other URLs returned unchanged. Malformed inputs returned as-is."""
    try:
        parts = urlsplit(value)
    except ValueError:
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
    except ValueError:
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
        except Exception:  # noqa: BLE001 - optional sanity checks must not abort setup
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
        except Exception as exc:  # noqa: BLE001 - retry arbitrary installer failures
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

    def __init__(
        self,
        *args: object,
        extra_env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> None:
        merged_extra_env = dict(extra_env or {})
        merged_extra_env.update(OPENCODE_RUNTIME_SECRETS)
        self._runtime_secret_keys = frozenset(OPENCODE_RUNTIME_SECRETS)
        super().__init__(*args, extra_env=merged_extra_env, **kwargs)

    async def install(self, environment: BaseEnvironment) -> None:
        async def _prepare_python_runtime() -> None:
            wheel_dir = self._extra_env.get(
                "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
            )
            await self.exec_as_root(
                environment,
                command=container_bootstrap.build_python_runtime_command(wheel_dir),
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
            except Exception:  # noqa: BLE001 - command failure means the tool is absent
                return False

        async def _run_local_opencode_install() -> None:
            # Prefer the monitor-prepared Node/OpenCode cache. This avoids 80
            # task containers all fetching nvm, nodejs.org, and npm in parallel.
            raw_version = getattr(self, "version", None)
            if callable(raw_version):
                raw_version = raw_version()
            version = str(raw_version or os.environ.get("OPENCODE_VERSION", "latest"))
            wheel_dir = self._extra_env.get(
                "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
            )
            archive_basename = f"opencode-ai-{version}.tgz"
            platform_basename = f"opencode-linux-x64-{version}.tgz"
            await self.exec_as_agent(
                environment,
                command=container_bootstrap.build_npm_tool_install_command(
                    container_bootstrap.NpmToolSpec(
                        executable="opencode",
                        package="opencode-ai",
                        version=version,
                        archive_path=self._extra_env.get("OPENCODE_TGZ_PATH", "")
                        or f"{wheel_dir}/{archive_basename}",
                        archive_url=self._extra_env.get(
                            "HARBOR_LOCAL_OPENCODE_TGZ_URL", ""
                        ),
                        archive_basename=archive_basename,
                        platform_archive_path=self._extra_env.get(
                            "OPENCODE_LINUX_X64_TGZ_PATH", ""
                        )
                        or f"{wheel_dir}/{platform_basename}",
                        platform_archive_url=self._extra_env.get(
                            "HARBOR_LOCAL_OPENCODE_LINUX_X64_TGZ_URL", ""
                        ),
                        platform_archive_basename=platform_basename,
                        npm_cache_dir=self._extra_env.get(
                            "CC_OPIK_NPM_CACHE_DIR", ""
                        )
                        or f"{wheel_dir}/npm-cache",
                        npm_registry=self._extra_env.get("NPM_CONFIG_REGISTRY", ""),
                    ),
                    wheel_dir=wheel_dir,
                    wheel_url=self._extra_env.get(
                        "HARBOR_LOCAL_WHEEL_SERVER_URL", ""
                    ),
                    node_dist_url=self._extra_env.get("CC_NODE_DIST_URL", ""),
                ),
            )

        await _retry_async(
            "local opencode install",
            _run_local_opencode_install,
            attempts=2,
            backoff_base_s=10.0,
            sanity_check=_opencode_present,
        )

        if not opik_tracing_enabled(getattr(self, "_extra_env", None)):
            print(
                "[opik-cold] Opik tracing disabled: skip hook dependencies "
                "and plugin files",
                flush=True,
            )
            return

        async def _run_hook_python_deps_install() -> None:
            # Keep the runtime install offline/cache-first. Only fall back to
            # public pip when neither the mounted cache nor the wheel HTTP
            # mirror is usable.
            wheel_dir = self._extra_env.get(
                "CC_OPIK_PY_WHEEL_DIR", "/opt/tb-opik/python-wheels"
            )
            await self.exec_as_agent(
                environment,
                command=container_bootstrap.build_python_dependencies_command(
                    ("opik", "uuid6", "socksio"),
                    wheel_dir=wheel_dir,
                    wheel_url=self._extra_env.get(
                        "HARBOR_LOCAL_WHEEL_SERVER_URL", ""
                    ),
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
        await environment.upload_file(TRACE_GATE_PY, "/tmp/opik_trace_gate.py")
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
                f'"$HOME/{CONTAINER_PLUGIN_REL}/finalize_opencode_sessions.py"; '
                "install -m 0644 /tmp/opik_trace_gate.py "
                f'"$HOME/{CONTAINER_PLUGIN_REL}/opik_trace_gate.py"'
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
        trace_enabled = opik_tracing_enabled(getattr(self, "_extra_env", None))

        if not self.model_name:
            raise ValueError("Model name must not be empty")

        env: dict[str, str] = {}

        # Harbor's Trial scopes `_extra_env` over every environment exec during
        # agent setup/run. Forward only non-secrets explicitly because `_exec`
        # records this per-command env in the trial log.
        for key, value in self._extra_env.items():
            if key not in self._runtime_secret_keys:
                env[key] = value

        if trace_enabled:
            # Localhost OPIK_URL on the host needs to become
            # host.docker.internal inside the container, otherwise the
            # in-container hook can't reach the local Opik backend.
            for key in ("OPIK_URL", "OPIK_URL_OVERRIDE"):
                if key in env:
                    env[key] = _rewrite_container_opik_url(env[key])

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
