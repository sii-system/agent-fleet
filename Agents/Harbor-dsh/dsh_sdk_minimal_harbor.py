"""Harbor adapter for DSH's version-matched ``sdk-minimal`` profile."""

from __future__ import annotations

import base64
import json
import math
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

HARBOR_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "utils" / "common" / "Harbor"
sys.path.append(str(HARBOR_RUNTIME_DIR))

from agent_output import exec_logged_agent  # noqa: E402


class AgentFleetDshSdkMinimal(BaseInstalledAgent):
    """Drive the official sdk-minimal profile through its Python JSON-RPC SDK."""

    _PYTHON_ROOT = "/opt/dsh-sdk-minimal-python3.12-runtime"
    _PYTHON = f"{_PYTHON_ROOT}/bin/python3.12"
    _SDK_ROOT = "/opt/dsh-sdk-minimal-runtime"
    _SITE_PACKAGES = f"{_SDK_ROOT}/site-packages"
    _DSH_HOME = "/logs/agent/dsh-home"
    _REMOTE_RUNNER = "/installed-agent/sdk_minimal.py"
    _REMOTE_SAMPLING_PLUGIN = "/installed-agent/dsh_sampling_plugin.mjs"
    _REMOTE_SERVICE_HANDOFF = "/installed-agent/dsh_service_handoff.py"
    _REMOTE_PATCH = "/installed-agent/dsh_sdk_minimal.cordis.yml"
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
        temperature: str | float = "1.0",
        top_p: str | float = "0.95",
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
        self._temperature = self._sampling_float(
            "temperature", temperature, minimum=0.0
        )
        self._top_p = self._sampling_float(
            "top_p", top_p, minimum=0.0, maximum=1.0, exclusive_minimum=True
        )

    @staticmethod
    def _positive_int(name: str, value: str | int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed <= 0 or str(parsed) != str(value).strip():
            raise ValueError(f"{name} must be positive")
        return parsed

    @staticmethod
    def _sampling_float(
        name: str,
        value: str | float,
        *,
        minimum: float,
        maximum: float | None = None,
        exclusive_minimum: bool = False,
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        below_minimum = parsed <= minimum if exclusive_minimum else parsed < minimum
        if (
            not math.isfinite(parsed)
            or below_minimum
            or (maximum is not None and parsed > maximum)
        ):
            interval = (
                f"({minimum}, {maximum}]"
                if exclusive_minimum
                else f"[{minimum}, {maximum or 'inf'}]"
            )
            raise ValueError(f"{name} must be in {interval}")
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
        value = self._get_env("BASE_URL")
        if not value:
            raise ValueError("BASE_URL is required")
        normalized = value.rstrip("/")
        for endpoint in ("/chat/completions", "/responses"):
            if normalized.endswith(endpoint):
                normalized = normalized[: -len(endpoint)]
                break
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        return normalized

    def _runtime_env(self, *, placeholder_key: bool = False) -> dict[str, str]:
        api_key = self._get_env("API_KEY")
        if not api_key and not placeholder_key:
            raise ValueError("API_KEY is required")
        return {
            "CI": "1",
            "DEEPSEEK_API_KEY": api_key or "config-dump-placeholder",
            "DEEPSEEK_BASE_URL": self._base_url(),
            "DSH_CONTEXT_WINDOW": str(self._context_window),
            "DSH_HOME": self._DSH_HOME,
            "DSH_MODEL": self._model_id(),
            "DSH_TEMPERATURE": str(self._temperature),
            "DSH_TOP_P": str(self._top_p),
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
            or os.environ.get("DSH_SDK_MINIMAL_CLI_VERSION", "0.1.3-alpha.1")
        )
        owner = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                "command -v bash >/dev/null && command -v tar >/dev/null && "
                "command -v base64 >/dev/null && "
                f"mkdir -p /installed-agent {self._DSH_HOME} /opt && "
                f"chown -R {shlex.quote(owner)} /installed-agent "
                f"{self._DSH_HOME} /logs/agent && "
                f"rm -rf {self._PYTHON_ROOT} {self._SDK_ROOT} && "
                'test -f "${DSH_PYTHON_RUNTIME_PATH}" && '
                'test -f "${DSH_SDK_MINIMAL_RUNTIME_TAR_PATH}" && '
                'test -f "${DSH_CLI_RUNTIME_PATH}" && '
                'tar -xzf "${DSH_PYTHON_RUNTIME_PATH}" -C /opt && '
                'tar -xzf "${DSH_SDK_MINIMAL_RUNTIME_TAR_PATH}" -C /opt'
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
                    "dsh-sdk-minimal-runtime-dsh-v0.1.3-alpha.1.tar.gz"
                ),
                "DSH_CLI_RUNTIME_PATH": self._get_env("DSH_CLI_RUNTIME_PATH")
                or f"/opt/agent-fleet/dsh-runtime/dsh-sdk-minimal-cli-runtime-{version}.tar.gz",
            },
        )
        await self.exec_as_agent(
            environment,
            command=(
                'set -euo pipefail; mkdir -p "$HOME/.local/bin"; '
                'tar -xzf "${DSH_CLI_RUNTIME_PATH}" -C "$HOME/.local"; '
                'export PATH="$HOME/.local/bin:$PATH"; '
                "dsh --version"
            ),
            env={
                "DSH_CLI_RUNTIME_PATH": self._get_env("DSH_CLI_RUNTIME_PATH")
                or f"/opt/agent-fleet/dsh-runtime/dsh-sdk-minimal-cli-runtime-{version}.tar.gz"
            },
        )
        for filename, remote in (
            ("dsh_sdk_minimal_runner.py", self._REMOTE_RUNNER),
            ("dsh_sampling_plugin.mjs", self._REMOTE_SAMPLING_PLUGIN),
            ("dsh_service_handoff.py", self._REMOTE_SERVICE_HANDOFF),
            ("dsh_sdk_minimal.cordis.yml", self._REMOTE_PATCH),
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
                f"dsh --profile sdk-minimal --patch {self._REMOTE_PATCH} "
                "--dump-config "
                "> /logs/agent/dsh-sdk-minimal-config-dump.yml 2>&1; "
                f"{{ {self.get_version_command()}; }} > "
                "/logs/agent/dsh-sdk-minimal-version.txt 2>&1"
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
        service_manifest = f"/tmp/{session_id}-services.json"
        service_control = f"/tmp/{session_id}-services.active"
        service_ready = f"/tmp/{session_id}-services.ready"
        service_snapshot_request = f"/tmp/{session_id}-services.snapshot-request"
        service_snapshot_ready = f"/tmp/{session_id}-services.snapshot-ready"
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
            "--patch",
            shlex.quote(self._REMOTE_PATCH),
            "--provider",
            "deepseek-official",
            "--model",
            shlex.quote(self._model_id()),
            "--reasoning-effort",
            "max",
            "--trace-path",
            f"/logs/agent/{self._TRACE_FILENAME}",
            "--service-snapshot-request",
            shlex.quote(service_snapshot_request),
            "--service-snapshot-ready",
            shlex.quote(service_snapshot_ready),
        ]
        if self._max_tokens is not None:
            runner_parts.extend(("--max-tokens", str(self._max_tokens)))
        runner = " ".join(runner_parts)
        output = f"/logs/agent/{self._OUTPUT_FILENAME}"
        script = f"""\
set -o pipefail
export PATH="$HOME/.local/bin:$PATH"
dsh_session_id={shlex.quote(session_id)}
printf '%s\n' "$PWD" > /logs/agent/dsh-workspace.txt
printf '%s\n' "$dsh_session_id" > /logs/agent/dsh-session-id.txt
capture_output() {{
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL tee "$@"
  else
    tee "$@"
  fi
}}
{runner} --session-id "$dsh_session_id" {shlex.quote(instruction)} 2>&1 \
  | capture_output {shlex.quote(output)}
"""
        await self.exec_as_agent(
            environment,
            command=(
                "set -eu; "
                f"rm -f {shlex.quote(service_manifest)} "
                f"{shlex.quote(service_ready)} "
                f"{shlex.quote(service_snapshot_request)} "
                f"{shlex.quote(service_snapshot_ready)}; "
                f": > {shlex.quote(service_control)}; "
                f"setsid {self._PYTHON} {self._REMOTE_SERVICE_HANDOFF} --watch "
                f"--control {shlex.quote(service_control)} "
                f"--manifest {shlex.quote(service_manifest)} "
                f"--ready {shlex.quote(service_ready)} "
                f"--snapshot-request {shlex.quote(service_snapshot_request)} "
                f"--snapshot-ready {shlex.quote(service_snapshot_ready)} "
                ">> /logs/agent/dsh-service-handoff-watch.log 2>&1 "
                "< /dev/null & watcher_pid=$!; "
                "for attempt in $(seq 1 100); do "
                f"[ -f {shlex.quote(service_ready)} ] && exit 0; "
                'if ! kill -0 "$watcher_pid" 2>/dev/null; then '
                f"rm -f {shlex.quote(service_control)}; exit 1; fi; "
                "sleep 0.1; done; "
                f"rm -f {shlex.quote(service_control)}; exit 1"
            ),
            env=self._runtime_env(),
        )
        try:
            await exec_logged_agent(
                self,
                environment,
                command=f"bash -lc {shlex.quote(script)}",
                env=self._runtime_env(),
            )
        finally:
            await self.exec_as_agent(
                environment,
                command=(
                    f"rm -f {shlex.quote(service_control)} "
                    f"{shlex.quote(service_ready)} "
                    f"{shlex.quote(service_snapshot_request)} "
                    f"{shlex.quote(service_snapshot_ready)}; "
                    "for attempt in $(seq 1 100); do "
                    f"[ -f {shlex.quote(service_manifest)} ] && break; "
                    "sleep 0.1; done; "
                    f"{self._PYTHON} {self._REMOTE_SERVICE_HANDOFF} "
                    f"--restore {shlex.quote(service_manifest)} "
                    "--receipt /logs/agent/dsh-service-handoff.json"
                ),
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
