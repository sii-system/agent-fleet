"""Validate the DSec Agent Toolkit capability before a Harbor batch starts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

_API_VERSION = "sandbox-lifecycle/v1alpha1"
_MAX_STATUS_BYTES = 1 << 20
_AGENT_VERSION_ENV = {
    "pi": "PI_VERSION",
    "claude-code": "CLAUDE_CODE_VERSION",
    "opencode": "OPENCODE_VERSION",
}


def required_agent_runtime(config: Mapping[str, str]) -> tuple[str, str] | None:
    """Return the exact runtime required by this Harbor invocation."""

    agent = config.get("AGENT", "").strip()
    version_env = _AGENT_VERSION_ENV.get(agent)
    if version_env is None:
        return None
    version = config.get(version_env, "").strip()
    if not version or version == "latest":
        raise ValueError(
            f"DSec Agent Toolkit requires an exact {version_env} version"
        )
    return agent, version


def validate_dsec_agent_runtime(
    config: Mapping[str, str], *, opener: Any | None = None
) -> str | None:
    """Require every schedulable DSec Node to expose one exact runtime."""

    required = required_agent_runtime(config)
    if required is None:
        return None
    endpoint = config.get("DSEC_SANDBOX_CONTROLLER_ENDPOINT", "").rstrip("/")
    request = Request(endpoint + "/v1alpha1/status", method="GET")
    transport = opener or build_opener(ProxyHandler({}))
    try:
        with transport.open(request, timeout=15) as response:
            payload = response.read(_MAX_STATUS_BYTES + 1)
    except OSError as error:
        raise ValueError("DSec Controller status preflight failed") from error
    if len(payload) > _MAX_STATUS_BYTES:
        raise ValueError("DSec Controller status response exceeds protocol limit")
    try:
        envelope = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("DSec Controller status response is invalid JSON") from error
    result = _validated_status_result(envelope)
    ready_nodes = [
        node
        for node in result["nodes"]
        if node.get("reachable") is True
        and isinstance(node.get("status"), dict)
        and node["status"].get("ready") is True
    ]
    if not ready_nodes:
        raise ValueError("DSec Controller has no schedulable ready Nodes")
    agent, version = required
    missing = [
        str(node.get("node_id") or "<unknown>")
        for node in ready_nodes
        if not _node_has_runtime(node["status"], agent, version)
    ]
    if missing:
        raise ValueError(
            f"DSec Agent Toolkit is missing {agent}@{version} on ready Nodes: "
            + ", ".join(missing)
        )
    return f"DSec Agent Toolkit: {agent}@{version} on {len(ready_nodes)} ready Nodes"


def _validated_status_result(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or envelope.get("api_version") != _API_VERSION:
        raise ValueError("DSec Controller returned an unsupported API version")
    result = envelope.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("nodes"), list):
        raise ValueError(  # noqa: TRY004 - protocol rejection is a launch error
            "DSec Controller status response is missing Nodes"
        )
    if not all(isinstance(node, dict) for node in result["nodes"]):
        raise ValueError("DSec Controller status response contains an invalid Node")
    return result


def _node_has_runtime(status: Mapping[str, Any], agent: str, version: str) -> bool:
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    runtimes = capabilities.get("agent_runtimes")
    if not isinstance(runtimes, list):
        return False
    return any(
        isinstance(runtime, dict)
        and runtime.get("agent") == agent
        and runtime.get("version") == version
        for runtime in runtimes
    )
