"""Harbor Pi adapter backed by Agent Fleet's pinned, cache-first runtime."""

from __future__ import annotations

import os
import shlex
from urllib.parse import urlparse

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.pi import Pi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


def _configured_value(extra_env: dict[str, str], name: str) -> str:
    return (extra_env.get(name, "") or os.environ.get(name, "")).strip()


def _resolve_provider_and_model(
    extra_env: dict[str, str], model_name: str | None
) -> tuple[str, str]:
    model = (model_name or "").strip()
    if not model:
        raise ValueError("Model name must not be empty")

    provider = _configured_value(extra_env, "PI_PROVIDER")
    if provider:
        # An explicit provider makes the model ID opaque. In particular, a
        # slash can be part of a gateway model name instead of a separator.
        return provider, model

    # Preserve the adapter's legacy direct-use contract. The shared Agent Fleet
    # launcher resolves PI_PROVIDER before importing this adapter, so its opaque
    # slash-containing model IDs take the explicit-provider branch above.
    if "/" in model:
        provider, model = model.split("/", 1)
        if not provider or not model:
            raise ValueError("Model name must be in the format provider/model_name")
        return provider, model

    for name in ("HARBOR_ANTHROPIC_BASE_URL", "BASE_URL"):
        base_url = _configured_value(extra_env, name)
        if not base_url:
            continue
        provider = (urlparse(base_url).hostname or "").lower()
        if not provider:
            raise ValueError(f"{name} must be an absolute URL")
        return provider, model

    raise ValueError(
        "PI_PROVIDER must not be empty; set PI_PROVIDER, use provider/model_name, "
        "or configure BASE_URL"
    )


class AgentFleetPi(Pi):
    """Run the Agent Fleet Pi fork with an isolated custom-provider config.

    Harbor's built-in Pi integration installs the upstream package through nvm
    for every fresh task image. Agent Fleet instead prepares a pinned,
    fully-installed Pi runtime and a portable Node runtime once on the runner.
    Task setup only extracts those immutable archives and never resolves npm
    dependencies inside the benchmark container.
    """

    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; pi --version'

    def build_cli_flags(self) -> str:
        # Reasoning strength is configured via settings.json (written from
        # PI_SETTINGS_CONFIG during install), mirroring opencode's
        # config-driven thinking instead of a wrapper-level CLI flag.
        #
        # Pi extensions (.ts) are host-mounted into $PI_EXTENSION_DIR and
        # collected inside the container's run() shell, because the mount
        # point does not exist on the host (see run()). Keep this empty so the
        # -e flags are only ever built where the files actually exist.
        return ""

    async def install(self, environment: BaseEnvironment) -> None:
        version = str(self.version() or os.environ.get("PI_VERSION", "0.81.1"))
        version_q = shlex.quote(version)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                f"pi_version={version_q}; "
                'cache_dir="${PI_CACHE_DIR:-/opt/tb-opik/python-wheels}"; '
                'pi_runtime_tar="${PI_RUNTIME_TAR_PATH:-$cache_dir/pi-runtime-${pi_version}.tar.gz}"; '
                'node_tar="${PI_NODE_RUNTIME_PATH:-$cache_dir/pi-node-runtime.tar.gz}"; '
                'mkdir -p "$HOME/.local/bin" "$HOME/.pi/agent"; '
                'mkdir -p "/logs/agent" 2>/dev/null || echo "cannot create /logs/agent; pi log capture disabled" >&2; '
                'if [ -f "$node_tar" ]; then '
                '  node_dir="$(mktemp -d /tmp/tb-pi-node-XXXXXX)"; '
                '  tar -xzf "$node_tar" -C "$node_dir"; '
                '  node_bin="$(find "$node_dir" -path "*/bin/node" -print -quit 2>/dev/null)"; '
                '  if [ -z "$node_bin" ]; then '
                "    echo 'cached Node runtime contains no node binary' >&2; "
                "    exit 1; "
                "  fi; "
                '  node_bin_dir="$(dirname "$node_bin")"; '
                '  ln -sf "$node_bin_dir/node" "$HOME/.local/bin/node"; '
                '  export PATH="$HOME/.local/bin:$node_bin_dir:$PATH"; '
                "fi; "
                'if ! command -v node >/dev/null 2>&1; then '
                "  echo 'Pi setup requires Node and the cached runtime is unavailable' >&2; "
                "  exit 1; "
                "fi; "
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]' 2>/dev/null || true)\"; "
                'if [ -z "$node_major" ] || [ "$node_major" -lt 20 ]; then '
                '  echo "Pi setup requires Node 20 or newer (found $(node --version 2>/dev/null || echo unknown))" >&2; '
                "  exit 1; "
                "fi; "
                'if [ ! -f "$pi_runtime_tar" ]; then '
                "  echo \"cached Pi runtime is unavailable: $pi_runtime_tar\" >&2; "
                "  exit 1; "
                "fi; "
                'tar -xzf "$pi_runtime_tar" -C "$HOME/.local"; '
                'printf "%s\\n" "$PI_MODELS_CONFIG" > "$HOME/.pi/agent/models.json"; '
                'printf "%s\\n" "$PI_SETTINGS_CONFIG" > "$HOME/.pi/agent/settings.json"; '
                "pi --version"
            ),
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        provider, model = _resolve_provider_and_model(
            self._extra_env, self.model_name
        )

        env = dict(self._extra_env)
        env.setdefault("PI_OFFLINE", "1")

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command, env=env)

        await self.exec_as_agent(
            environment,
            command=(
                'set -o pipefail; export PATH="$HOME/.local/bin:$PATH"; '
                'ext_args=(); '
                # Collect any bind-mounted Pi extensions inside the container:
                # the mount point ($PI_EXTENSION_DIR) exists only here, so the
                # -e flags cannot be built on the host.
                "if [[ -n \"${PI_EXTENSION_DIR:-}\" && -d \"$PI_EXTENSION_DIR\" ]]; then "
                '  for ext in "$PI_EXTENSION_DIR"/*.ts; do '
                '    [[ -f "$ext" ]] && ext_args+=( -e "$ext" ); '
                '  done; '
                "fi; "
                f"printf '%s' {shlex.quote(instruction)} | "
                "pi --print --mode json --no-session "
                f"--provider {shlex.quote(provider)} "
                f"--model {shlex.quote(model)} "
                '"${ext_args[@]:-}" '
                "2>&1 "
                "| { grep -v '\"type\":\"message_update\"' || true; } "
                f"| {{ if command -v stdbuf >/dev/null 2>&1; then "
                f"stdbuf -oL tee /logs/agent/{self._OUTPUT_FILENAME}; "
                f"else tee /logs/agent/{self._OUTPUT_FILENAME}; fi }}"
            ),
            env=env,
        )
