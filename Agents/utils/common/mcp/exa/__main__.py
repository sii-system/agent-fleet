"""Expose Exa's hosted MCP through Agent Fleet's existing stdio interface."""

import json
import os
import sys
import urllib.request

from config import ANONYMOUS_KEY, API_KEY_HEADER, MCP_URL

LOCAL_TO_REMOTE = {
    "web_search": "web_search_exa",
    "web_fetch": "web_fetch_exa",
}
REMOTE_TO_LOCAL = {value: key for key, value in LOCAL_TO_REMOTE.items()}


def _decode(body: str) -> dict:
    if not body.startswith("event:"):
        return json.loads(body)
    for event in body.split("\n\n"):
        data = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
        if data and data != ["[DONE]"]:
            return json.loads("\n".join(data))
    raise ValueError("hosted MCP returned no JSON-RPC message")


def _request(message: dict) -> dict:
    key = os.environ.get("EXA_API_KEY", ANONYMOUS_KEY).strip()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
        "User-Agent": "agent-fleet-exa-mcp/1",
    }
    if key and key != ANONYMOUS_KEY:
        headers[API_KEY_HEADER] = key
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps(message).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return _decode(response.read().decode())


def _forward(message: dict) -> dict:
    outgoing = dict(message)
    if message.get("method") == "tools/call":
        outgoing["params"] = dict(message.get("params") or {})
        name = outgoing["params"].get("name")
        outgoing["params"]["name"] = LOCAL_TO_REMOTE.get(name, name)
    result = _request(outgoing)
    if message.get("method") == "tools/list":
        for tool in result.get("result", {}).get("tools", []):
            tool["name"] = REMOTE_TO_LOCAL.get(tool.get("name"), tool.get("name"))
            tool["description"] = tool.get("description", "").replace("_exa", "")
    return result


def main() -> None:
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            response = _forward(message)
        except Exception as exc:  # noqa: BLE001 - keep serving after a bad request.
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32000, "message": str(exc)},
            }
        print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
