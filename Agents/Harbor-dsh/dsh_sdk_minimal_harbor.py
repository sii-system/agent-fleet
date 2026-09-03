"""Harbor adapter for DSH's version-matched ``sdk-minimal`` profile."""

from __future__ import annotations

import base64
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class AgentFleetDshSdkMinimal(BaseInstalledAgent):
    """Drive the official sdk-minimal profile through its Python JSON-RPC SDK."""

    _PYTHON_ROOT = "/opt/dsh-sdk-minimal-python3.12-runtime"
    _PYTHON = f"{_PYTHON_ROOT}/bin/python3.12"
    _SDK_ROOT = "/opt/dsh-sdk-minimal-runtime"
    _SITE_PACKAGES = f"{_SDK_ROOT}/site-packages"
    _NODE_HOME = "/installed-agent/dsh-node"
    _DSH_HOME = "/logs/agent/dsh-home"
    _REMOTE_RUNNER = "/installed-agent/sdk_minimal.py"
    _REMOTE_RELAY = "/installed-agent/dsh_sampling_relay.py"
    _RELAY_PORT = 18100
    _OUTPUT_FILENAME = "dsh-sdk-minimal.txt"
    _TRACE_FILENAME = "dsh-sdk-minimal-trace.jsonl"

    @staticmethod
    @override
    def name() -> str:
        return "dsh-sdk-minimal"

    def __init__(
        self,
        *args: Any,
        permission_mode: str = "danger-full-access",
        provider_route: str = "deepseek",
        context_window: str | int = "200000",
        max_tokens: str | int | None = "65536",
        process_retry_max: str | int = "0",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if permission_mode != "danger-full-access":
            raise ValueError("dsh-sdk-minimal requires danger-full-access")
        if provider_route != "deepseek":
            raise ValueError(
                "dsh-sdk-minimal supports the native DeepSeek provider only"
            )
        if self.skills_dir or self.mcp_servers:
            raise ValueError("dsh-sdk-minimal does not support Skills or MCP servers")
        self._context_window = self._positive_int("context_window", context_window)
        self._max_tokens = (
            None
            if max_tokens in (None, "")
            else self._positive_int("max_tokens", max_tokens)
        )
        self._process_retry_max = self._nonnegative_int(
            "process_retry_max", process_retry_max
        )

    @staticmethod
    def _positive_int(name: str, value: str | int) -> int:
        parsed = AgentFleetDshSdkMinimal._nonnegative_int(name, value)
        if parsed == 0:
            raise ValueError(f"{name} must be positive")
        return parsed

    @staticmethod
    def _nonnegative_int(name: str, value: str | int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < 0 or str(parsed) != str(value).strip():
            raise ValueError(f"{name} must be nonnegative")
        return parsed

    def _model_id(self) -> str:
        if not self.model_name or not self.model_name.startswith("deepseek/"):
            raise ValueError(
                "dsh-sdk-minimal model name must be deepseek/<wire-model-id>"
            )
        model_id = self.model_name.split("/", 1)[1]
        if not model_id:
            raise ValueError("dsh-sdk-minimal model ID cannot be empty")
        return model_id

    def _base_url(self) -> str:
        value = self._get_env("DSH_BASE_URL")
        if not value:
            raise ValueError("DSH_BASE_URL is required")
        normalized = value.rstrip("/")
        for endpoint in ("/chat/completions", "/responses"):
            if normalized.endswith(endpoint):
                normalized = normalized[: -len(endpoint)]
                break
        return normalized

    def _runtime_env(self, *, placeholder_key: bool = False) -> dict[str, str]:
        api_key = self._get_env("DSH_API_KEY")
        if not api_key and not placeholder_key:
            raise ValueError("DSH_API_KEY is required")
        return {
            "CI": "1",
            "DEEPSEEK_API_KEY": api_key or "config-dump-placeholder",
            "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{self._RELAY_PORT}/v1",
            "DSH_CONTEXT_WINDOW": str(self._context_window),
            "DSH_HOME": self._DSH_HOME,
            "DSH_MODEL": self._model_id(),
            "DSH_SAMPLING_RELAY_PORT": str(self._RELAY_PORT),
            "DSH_SAMPLING_RECEIPT_PATH": "/logs/agent/sampling-relay.jsonl",
            "DSH_SAMPLING_UPSTREAM_BASE_URL": self._base_url(),
            "DSH_TELEMETRY_DISABLED": "1",
            "EDITOR": "true",
            "GIT_EDITOR": "true",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PAGER": "cat",
            "PYTHONPATH": self._SITE_PACKAGES,
        }

    @staticmethod
    def _encoded(content: str) -> str:
        return base64.b64encode(content.encode()).decode("ascii")

    async def _upload_text(
        self,
        environment: BaseEnvironment,
        *,
        content: str,
        path: str,
    ) -> None:
        await self.exec_as_agent(
            environment,
            command=(
                f"printf %s {shlex.quote(self._encoded(content))} | "
                f"base64 -d > {shlex.quote(path)}"
            ),
        )

    @override
    def get_version_command(self) -> str:
        return (
            'export PATH="$HOME/.local/bin:$PATH"; '
            "printf 'dsh='; dsh --version; "
            f"printf 'sdk-source='; cat {self._SDK_ROOT}/SOURCE_VERSION"
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        version = str(
            self.version()
            or os.environ.get("DSH_SDK_MINIMAL_CLI_VERSION", "0.1.2-alpha.2")
        )
        owner = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                "command -v bash >/dev/null && command -v tar >/dev/null && "
                "command -v base64 >/dev/null && "
                f"mkdir -p /installed-agent {self._NODE_HOME} {self._DSH_HOME} /opt && "
                f"chown -R {shlex.quote(owner)} /installed-agent {self._NODE_HOME} "
                f"{self._DSH_HOME} /logs/agent && "
                f"rm -rf {self._PYTHON_ROOT} {self._SDK_ROOT} {self._NODE_HOME}/* && "
                'test -f "${DSH_PYTHON_RUNTIME_PATH}" && '
                'test -f "${DSH_SDK_MINIMAL_RUNTIME_TAR_PATH}" && '
                'test -f "${DSH_NODE_RUNTIME_PATH}" && '
                'test -f "${DSH_CLI_RUNTIME_PATH}" && '
                'tar -xzf "${DSH_PYTHON_RUNTIME_PATH}" -C /opt && '
                'tar -xzf "${DSH_SDK_MINIMAL_RUNTIME_TAR_PATH}" -C /opt && '
                f'tar -xzf "${{DSH_NODE_RUNTIME_PATH}}" -C {self._NODE_HOME} '
                "--strip-components=1"
            ),
            env={
                "DSH_PYTHON_RUNTIME_PATH": self._get_env("DSH_PYTHON_RUNTIME_PATH")
                or (
                    "/opt/agent-fleet/dsh-runtime/"
                    "dsh-sdk-minimal-python3.12-runtime.tar.gz"
                ),
                "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH": self._get_env(
                    "DSH_SDK_MINIMAL_RUNTIME_TAR_PATH"
                )
                or (
                    "/opt/agent-fleet/dsh-runtime/"
                    "dsh-sdk-minimal-runtime-dsh-v0.1.2-alpha.2.tar.gz"
                ),
                "DSH_NODE_RUNTIME_PATH": self._get_env("DSH_NODE_RUNTIME_PATH")
                or "/opt/agent-fleet/dsh-runtime/node-runtime.tar.gz",
                "DSH_CLI_RUNTIME_PATH": self._get_env("DSH_CLI_RUNTIME_PATH")
                or f"/opt/agent-fleet/dsh-runtime/dsh-sdk-minimal-cli-runtime-{version}.tar.gz",
            },
        )
        await self.exec_as_agent(
            environment,
            command=(
                'set -euo pipefail; mkdir -p "$HOME/.local/bin"; '
                f'ln -sf {self._NODE_HOME}/bin/node "$HOME/.local/bin/node"; '
                f'ln -sf {self._NODE_HOME}/bin/npm "$HOME/.local/bin/npm"; '
                f'ln -sf {self._NODE_HOME}/bin/npx "$HOME/.local/bin/npx"; '
                'tar -xzf "${DSH_CLI_RUNTIME_PATH}" -C "$HOME/.local"; '
                'export PATH="$HOME/.local/bin:$PATH"; '
                'test "$(node -p \'process.versions.node.split(".")[0]\')" -ge 22; '
                "dsh --version"
            ),
            env={
                "DSH_CLI_RUNTIME_PATH": self._get_env("DSH_CLI_RUNTIME_PATH")
                or f"/opt/agent-fleet/dsh-runtime/dsh-sdk-minimal-cli-runtime-{version}.tar.gz"
            },
        )
        for filename, remote in (
            ("dsh_sdk_minimal_runner.py", self._REMOTE_RUNNER),
            ("dsh_sampling_relay.py", self._REMOTE_RELAY),
        ):
            await self._upload_text(
                environment,
                content=Path(__file__).with_name(filename).read_text(encoding="utf-8"),
                path=remote,
            )
        await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                "dsh --profile sdk-minimal --dump-config "
                "> /logs/agent/dsh-sdk-minimal-config-dump.yml 2>&1; "
                f"{self.get_version_command()} > "
                "/logs/agent/dsh-sdk-minimal-version.txt"
            ),
            env=self._runtime_env(placeholder_key=True),
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        session_id = f"harbor-{uuid.uuid4().hex}"
        runner_parts = [
            shlex.quote(self._PYTHON),
            shlex.quote(self._REMOTE_RUNNER),
            '--workspace "$PWD"',
            "--dsh-home",
            shlex.quote(self._DSH_HOME),
            "--dsh-bin",
            '"$HOME/.local/bin/dsh"',
            "--profile",
            "sdk-minimal",
            "--provider",
            "deepseek-official",
            "--model",
            shlex.quote(self._model_id()),
            "--reasoning-effort",
            "max",
            "--trace-path",
            f"/logs/agent/{self._TRACE_FILENAME}",
        ]
        if self._max_tokens is not None:
            runner_parts.extend(("--max-tokens", str(self._max_tokens)))
        runner_parts.extend(('--session-id "$1"', shlex.quote(instruction)))
        runner = " ".join(runner_parts)
        output = f"/logs/agent/{self._OUTPUT_FILENAME}"
        script = f"""\
set -o pipefail
export PATH="$HOME/.local/bin:{self._NODE_HOME}/bin:$PATH"
relay_log=/logs/agent/sampling-relay.log
dsh_session_id={shlex.quote(session_id)}
printf '%s\n' "$PWD" > /logs/agent/dsh-workspace.txt
printf '%s\n' "$dsh_session_id" > /logs/agent/dsh-session-id.txt
{self._PYTHON} {self._REMOTE_RELAY} >>"$relay_log" 2>&1 &
relay_pid=$!
cleanup_relay() {{
  kill "$relay_pid" 2>/dev/null || true
  wait "$relay_pid" 2>/dev/null || true
}}
trap cleanup_relay EXIT INT TERM
relay_ready=0
for attempt in {{1..50}}; do
  if {self._PYTHON} -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:{self._RELAY_PORT}/healthz", timeout=1).read()'; then
    relay_ready=1
    break
  fi
  sleep 0.1
done
if (( relay_ready == 0 )); then
  echo "sampling relay failed to become ready" >&2
  exit 70
fi
run_dsh() {{
  {runner}
}}
capture_output() {{
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL tee "$@"
  else
    tee "$@"
  fi
}}
retry=0
while true; do
  attempt_session_id="$dsh_session_id"
  if (( retry > 0 )); then
    attempt_session_id="$dsh_session_id-retry-$retry"
  fi
  if (( retry == 0 )); then
    run_dsh "$attempt_session_id" 2>&1 | capture_output {shlex.quote(output)}
  else
    run_dsh "$attempt_session_id" 2>&1 | capture_output -a {shlex.quote(output)}
  fi
  status=${{PIPESTATUS[0]}}
  if (( status == 0 || retry >= {self._process_retry_max} )); then
    exit "$status"
  fi
  retry=$((retry + 1))
  delay=$((retry * 5))
  printf 'agent-fleet: restarting dsh-sdk-minimal process after exit %s (retry %s/%s, delay %ss)\n' \
    "$status" "$retry" "{self._process_retry_max}" "$delay" >&2
  sleep "$delay"
done
"""
        await self.exec_as_agent(
            environment,
            command=f"bash -lc {shlex.quote(script)}",
            env=self._runtime_env(),
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        root = self.logs_dir / "dsh-home" / "sessions"
        if not root.is_dir():
            return
        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0
        for path in root.rglob("session.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant/message":
                    continue
                data = event.get("data")
                usage = data.get("usage") if isinstance(data, dict) else None
                if not isinstance(usage, dict):
                    continue
                cache_read = int(usage.get("cacheReadTokens") or 0)
                cache_write = int(usage.get("cacheWriteTokens") or 0)
                input_tokens += int(usage.get("inputTokens") or 0)
                input_tokens += cache_read + cache_write
                cache_tokens += cache_read
                output_tokens += int(usage.get("outputTokens") or 0)
        context.n_input_tokens = input_tokens or None
        context.n_output_tokens = output_tokens or None
        context.n_cache_tokens = cache_tokens or None
