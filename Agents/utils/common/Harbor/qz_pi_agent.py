"""qz variant of Harbor's Pi agent, for direct `harbor run` use:

    BASE_URL=<gateway-url> API_KEY=<gateway-key> \
      harbor run -a qz_pi_agent:QzPi -m <model> ...

Two deltas against the stock agent:

- install Node + Pi inside the Sandbox from configurable dist/npm sources,
  defaulting to npmmirror for regional stability;
- talk to the SII model gateway through a custom pi provider (models.json in
  PI_CODING_AGENT_DIR), contract copied from scripts/pi_prompt.py.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import override

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.pi import Pi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

NODE_VERSION = "v22.14.0"
DEFAULT_NODE_DIST_URL = (
    "https://registry.npmmirror.com/-/binary/node/"
    f"{NODE_VERSION}/node-{NODE_VERSION}-linux-x64.tar.gz"
)
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"
PROVIDER = "qzgw"
AGENT_DIR = "/tmp/pi-agent-dir"
MODELS_PATH = f"{AGENT_DIR}/models.json"


class QzPi(Pi):
    def __init__(
        self,
        *args,
        base_url: str = "",
        api_key: str = "",
        node_dist_url: str = "",
        npm_registry: str = "",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        url = (base_url or os.environ.get("BASE_URL", "")).strip().rstrip("/")
        if url and not url.endswith("/v1"):
            url = f"{url}/v1"
        self._qz_base_url = url
        self._qz_api_key = (api_key or os.environ.get("API_KEY", "")).strip()
        self._node_dist_url = (
            node_dist_url
            or os.environ.get("QZ_NODE_DIST_URL", "")
            or os.environ.get("HARBOR_CC_NODE_DIST_URL", "")
            or DEFAULT_NODE_DIST_URL
        ).strip()
        self._npm_registry = (
            npm_registry
            or os.environ.get("NPM_CONFIG_REGISTRY", "")
            or DEFAULT_NPM_REGISTRY
        ).strip()
        if not self._qz_base_url or not self._qz_api_key:
            raise ValueError("QzPi requires base_url and api_key agent kwargs")

    @override
    def get_version_command(self) -> str | None:
        return "pi --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        version_spec = f"@{self._version}" if self._version else "@latest"
        node_dist_url = shlex.quote(self._node_dist_url)
        npm_registry = shlex.quote(self._npm_registry)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                f"curl -fsSL {node_dist_url} -o /tmp/node.tgz && "
                "mkdir -p /usr/local/node && "
                "tar -xzf /tmp/node.tgz -C /usr/local/node --strip-components=1 && "
                "ln -sf /usr/local/node/bin/node /usr/local/bin/node && "
                "ln -sf /usr/local/node/bin/npm /usr/local/bin/npm && "
                f"npm install -g --registry {npm_registry} "
                f"@mariozechner/pi-coding-agent{version_spec} && "
                "ln -sf /usr/local/node/bin/pi /usr/local/bin/pi && "
                "pi --version"
            ),
        )

    def _models_config(self) -> dict:
        # Provider contract mirrors scripts/pi_prompt.py (SII gateway / GLM).
        return {
            "providers": {
                PROVIDER: {
                    "baseUrl": self._qz_base_url,
                    "api": "openai-completions",
                    # Literal value: this pi build did not resolve a "$VAR"
                    # reference here (gateway saw a bad key). The file lives
                    # inside the throwaway sandbox only.
                    "apiKey": self._qz_api_key,
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                        "supportsUsageInStreaming": True,
                        "maxTokensField": "max_tokens",
                        "thinkingFormat": "zai",
                    },
                    "models": [
                        {
                            "id": self.model_name,
                            "name": "qz smoke model",
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": 204800,
                            "maxTokens": 32768,
                            "cost": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                            },
                        }
                    ],
                }
            }
        }

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("QzPi requires a model name (-m)")

        await self.exec_as_agent(
            environment,
            command=f"mkdir -p {AGENT_DIR}",
        )
        # BaseInstalledAgent logs every exec command and its env to trial.log.
        # Upload the credential-bearing config as a file so the gateway key
        # never appears in either logged channel.
        with tempfile.TemporaryDirectory(prefix="qz-pi-models-") as temp_dir:
            local_models = Path(temp_dir) / "models.json"
            local_models.write_text(
                json.dumps(self._models_config(), separators=(",", ":")),
                encoding="utf-8",
            )
            local_models.chmod(0o600)
            await environment.upload_file(local_models, MODELS_PATH)

        ownership = f"chmod 600 {MODELS_PATH}"
        if environment.default_user is not None:
            ownership = (
                f"chown {shlex.quote(str(environment.default_user))} {MODELS_PATH} && "
                f"{ownership}"
            )
        await self.exec_as_root(environment, command=ownership)

        cli_flags = self.build_cli_flags()
        if cli_flags:
            cli_flags += " "
        await self.exec_as_agent(
            environment,
            command=(
                "set -o pipefail; "
                "pi --print --mode json --no-session "
                f"--provider {PROVIDER} --model {shlex.quote(self.model_name)} "
                f"{cli_flags}"
                f"{shlex.quote(instruction)} "
                "2>&1 </dev/null | grep -v '\"type\":\"message_update\"' | "
                f"stdbuf -oL tee /logs/agent/{self._OUTPUT_FILENAME}"
            ),
            env={
                "PI_CODING_AGENT_DIR": AGENT_DIR,
                "PI_OFFLINE": "1",
            },
        )
