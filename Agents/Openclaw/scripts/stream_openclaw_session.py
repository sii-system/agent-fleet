#!/usr/bin/env python3
"""Summarize OpenClaw sessions from a local store root or a running container.

Discovers session metadata and transcript events, then selects the "best"
session per instance using a priority heuristic:

    active multi-turn (non-heartbeat)
    > active zero-turn (non-heartbeat)
    > recent non-heartbeat (any status)
    > active multi-turn (heartbeat-only)
    > idle

Can read sessions from either a mounted config directory (``--store-root``)
or directly from a Docker container (``--container-name``).

Usage::

    echo '{}' | python stream_openclaw_session.py --instance 1 --port 18789 --store-root /path/to/1
    echo '{}' | python stream_openclaw_session.py --instance 1 --port 18789 --container-name openclaw-1
    python stream_openclaw_session.py --instance 1 --port 18789 --store-root /path/to/1 --pretty
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote as shlex_quote
from typing import Any

ACTIVE_STATUSES = {"active", "running", "live", "open", "busy"}
HEARTBEAT_PREFIX = "Read HEARTBEAT.md if it exists"
CONTAINER_TRANSCRIPT_TAIL_LINES = 200


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize session payload into a flat list of session dicts.

    Handles three upstream formats: a plain list, a dict with a ``sessions``
    key, or a dict whose values are themselves session dicts.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if payload and all(isinstance(value, dict) for value in payload.values()):
            return [value for value in payload.values() if isinstance(value, dict)]
        sessions = payload.get("sessions")
        if isinstance(sessions, list):
            return [item for item in sessions if isinstance(item, dict)]
    return []


def _turn_count(session: dict[str, Any]) -> int:
    """Extract the turn/message count from a session dict, checking common key names."""
    for key in ("turnCount", "turn_count", "messageCount", "message_count"):
        value = session.get(key)
        if isinstance(value, int):
            return value
    for key in ("turns", "messages"):
        value = session.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _event_text(message: dict[str, Any]) -> str:
    """Extract human-readable text from a message event's content blocks or errorMessage."""
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return " ".join(parts)
    error_message = message.get("errorMessage")
    if isinstance(error_message, str) and error_message.strip():
        return error_message.strip()
    return ""


def _looks_like_heartbeat_text(text: str) -> bool:
    """Return True if *text* begins with the HEARTBEAT.md prompt prefix."""
    return text.strip().startswith(HEARTBEAT_PREFIX)


def _status(session: dict[str, Any]) -> str:
    """Return the normalized status string (lowercase) from a session dict."""
    for key in ("status", "state", "activity"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "unknown"


def _summary_text(session: dict[str, Any]) -> str:
    """Return the latest turn summary text, falling back to the last two turns' content."""
    for key in (
        "latestTurnsSummary",
        "latest_turns_summary",
        "latestTurnSummary",
        "latest_turn_summary",
        "summary",
    ):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    turns = session.get("turns")
    if isinstance(turns, list) and turns:
        snippets: list[str] = []
        for turn in turns[-2:]:
            if isinstance(turn, dict):
                text = turn.get("summary") or turn.get("content") or turn.get("text")
                if isinstance(text, str) and text.strip():
                    snippets.append(text.strip())
        if snippets:
            return " | ".join(snippets)

    return "No latest turn summary available."


def _session_id(session: dict[str, Any]) -> str:
    """Return the session identifier, or ``'(unknown)'`` if absent."""
    for key in ("id", "sessionId", "session_id"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(unknown)"


def _target(session: dict[str, Any]) -> str:
    """Return the session target/recipient (phone, label, etc.), or ``'-'`` if absent."""
    for key in ("target", "to", "recipient", "phone", "lastTo"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    origin = session.get("origin")
    if isinstance(origin, dict):
        for key in ("to", "label"):
            value = origin.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "-"


def _updated_at(session: dict[str, Any]) -> str:
    """Return the last-updated timestamp as an ISO string, or ``'-'`` if absent."""
    for key in ("updatedAt", "updated_at", "lastUpdatedAt", "last_updated_at", "createdAt", "created_at"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")
    return "-"


def _sort_key(session: dict[str, Any]) -> tuple[int, str]:
    """Return a (has_timestamp, iso_string) tuple for chronological sorting."""
    raw = _updated_at(session)
    if raw == "-":
        return (0, "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (1, dt.isoformat())
    except ValueError:
        return (1, raw)


def parse_session_events(jsonl_text: str) -> dict[str, Any]:
    """Parse a JSONL transcript and extract turn count, latest summary, and heartbeat status.

    Returns a dict with keys ``turn_count``, ``latest_turns_summary``,
    ``updated_at``, and ``is_heartbeat``.
    """
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    updated_at = "-"

    for raw_line in jsonl_text.splitlines():
        line = raw_line.strip()
        if not line:
          continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        timestamp = event.get("timestamp")
        if isinstance(timestamp, str) and timestamp.strip():
            updated_at = timestamp.strip()

        if event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _event_text(message)
        if role == "user" and text:
            user_messages.append(text)
        elif role == "assistant":
            if text:
                assistant_messages.append(text)
            elif isinstance(message.get("errorMessage"), str) and message["errorMessage"].strip():
                assistant_messages.append(message["errorMessage"].strip())

    latest_user = user_messages[-1] if user_messages else ""
    latest_assistant = assistant_messages[-1] if assistant_messages else ""
    parts = []
    if latest_user:
        parts.append(f"User: {latest_user}")
    if latest_assistant:
        parts.append(f"Assistant: {latest_assistant}")

    return {
        "turn_count": len(user_messages),
        "latest_turns_summary": " | ".join(parts) if parts else "No latest turn summary available.",
        "updated_at": updated_at,
        "is_heartbeat": bool(latest_user and _looks_like_heartbeat_text(latest_user)),
    }


def _normalize_session_file_path(root_dir: Path, session_file: str) -> Path:
    """Resolve a ``sessionFile`` value to a local path under *root_dir*.

    Strips the ``/agents/...`` prefix from absolute container paths so the
    result lands inside *root_dir*/agents/.
    """
    marker = "/agents/"
    if marker in session_file:
        suffix = session_file.split(marker, 1)[1]
        return root_dir / "agents" / suffix
    return root_dir / Path(session_file).name


def load_sessions_from_store_root(root_dir: str | Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load session metadata and transcript events from a mounted config directory.

    Scans ``agents/*/sessions/sessions.json`` under *root_dir*, reads each
    session's JSONL transcript, and returns ``(payload, event_lookup)``.
    """
    root = Path(root_dir)
    payload_sessions: list[dict[str, Any]] = []
    event_lookup: dict[str, dict[str, Any]] = {}

    for sessions_json in sorted(root.glob("agents/*/sessions/sessions.json")):
        try:
            payload = json.loads(sessions_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        for session in _as_list(payload):
            payload_sessions.append(session)
            session_id = _session_id(session)
            if session_id == "(unknown)":
                continue

            session_file_path = None
            raw_session_file = session.get("sessionFile")
            if isinstance(raw_session_file, str) and raw_session_file.strip():
                candidate = _normalize_session_file_path(root, raw_session_file.strip())
                if candidate.exists():
                    session_file_path = candidate
            if session_file_path is None:
                fallback = sessions_json.parent / f"{session_id}.jsonl"
                if fallback.exists():
                    session_file_path = fallback
            if session_file_path is None:
                continue

            try:
                event_lookup[session_id] = parse_session_events(
                    session_file_path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue

    return {"sessions": payload_sessions}, event_lookup


def _read_text_from_container(container_name: str, path: str) -> str:
    """Read a file's contents from a Docker container via ``docker exec cat``."""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"cat {shlex_quote(path)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to read {path} from {container_name}")
    return proc.stdout


def _read_transcript_tail_from_container(container_name: str, path: str) -> str:
    """Read the last N lines of a transcript file inside a Docker container."""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"tail -n {CONTAINER_TRANSCRIPT_TAIL_LINES} {shlex_quote(path)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to read {path} from {container_name}")
    return proc.stdout


def _list_container_session_stores(container_name: str) -> list[str]:
    """Return paths to all ``sessions.json`` files inside a container's ``~/.openclaw/agents``."""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            "find /home/node/.openclaw/agents -maxdepth 3 -path '*/sessions/sessions.json' | sort",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to list sessions stores in {container_name}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def load_sessions_from_container(container_name: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load session metadata and transcript events from a running Docker container.

    Uses ``docker exec`` to discover session stores and read transcripts.
    Returns ``(payload, event_lookup)``.
    """
    sessions: list[dict[str, Any]] = []
    event_lookup: dict[str, dict[str, Any]] = {}

    for store_path in _list_container_session_stores(container_name):
        try:
            payload = json.loads(_read_text_from_container(container_name, store_path))
        except (RuntimeError, json.JSONDecodeError):
            continue

        parts = Path(store_path).parts
        agent_id = "main"
        if "agents" in parts:
            idx = parts.index("agents")
            if idx + 1 < len(parts):
                agent_id = parts[idx + 1]

        for session in _as_list(payload):
            if not isinstance(session.get("agentId"), str) or not str(session.get("agentId")).strip():
                session["agentId"] = agent_id
            sessions.append(session)
            session_id = _session_id(session)
            if session_id == "(unknown)":
                continue

            session_file_path = None
            raw_session_file = session.get("sessionFile")
            if isinstance(raw_session_file, str) and raw_session_file.strip():
                session_file_path = raw_session_file.strip()
            else:
                session_file_path = f"/home/node/.openclaw/agents/{agent_id}/sessions/{session_id}.jsonl"

            try:
                event_lookup[session_id] = parse_session_events(
                    _read_transcript_tail_from_container(container_name, session_file_path)
                )
            except RuntimeError:
                continue

    return {"sessions": sessions}, event_lookup


def summarize_instance(
    instance: int,
    port: int,
    store_root: str | Path | None = None,
    container_name: str | None = None,
) -> dict[str, Any]:
    """Summarize the best session for a single OpenClaw instance.

    Prefers reading from *store_root* (local mount); falls back to
    *container_name* (Docker exec) if the store root is unavailable.
    """
    payload: dict[str, Any] = {"sessions": []}
    event_lookup: dict[str, dict[str, Any]] = {}

    if store_root is not None and Path(store_root).is_dir():
        payload, event_lookup = load_sessions_from_store_root(store_root)
    elif container_name:
        try:
            payload, event_lookup = load_sessions_from_container(container_name)
        except RuntimeError:
            payload, event_lookup = {"sessions": []}, {}

    return summarize_sessions_payload(
        payload,
        instance=instance,
        port=port,
        event_lookup=event_lookup,
    )


def summarize_sessions_payload(
    payload: Any,
    instance: int,
    port: int,
    event_lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select the best session from a raw payload using the priority heuristic.

    Priority order:
      1. Active multi-turn, non-heartbeat
      2. Active zero-turn, non-heartbeat
      3. Recent non-heartbeat (any status with turns > 0)
      4. Active multi-turn (heartbeat-only, returns idle)
      5. Idle (no qualifying sessions)

    Within each category the most recently updated session wins.
    """
    sessions = _as_list(payload)
    normalized: list[dict[str, Any]] = []
    for session in sessions:
        session_id = _session_id(session)
        event_data = (event_lookup or {}).get(session_id, {})
        status = _status(session)
        turn_count = _turn_count(session)
        if turn_count == 0 and isinstance(event_data.get("turn_count"), int):
            turn_count = int(event_data["turn_count"])
        latest_turns_summary = _summary_text(session)
        if latest_turns_summary == "No latest turn summary available." and isinstance(
            event_data.get("latest_turns_summary"), str
        ):
            latest_turns_summary = event_data["latest_turns_summary"]
        updated_at = _updated_at(session)
        if updated_at == "-" and isinstance(event_data.get("updated_at"), str):
            updated_at = event_data["updated_at"]
        is_heartbeat = False
        if isinstance(event_data.get("is_heartbeat"), bool):
            is_heartbeat = event_data["is_heartbeat"]
        elif _looks_like_heartbeat_text(latest_turns_summary):
            is_heartbeat = True
        if (
            latest_turns_summary == "No latest turn summary available."
            and (status in ACTIVE_STATUSES or status.startswith("active"))
        ):
            latest_turns_summary = "Session is running; transcript not flushed yet."
        normalized.append(
            {
                "raw": session,
                "status": status,
                "turn_count": turn_count,
                "session_id": session_id,
                "target": _target(session),
                "updated_at": updated_at,
                "latest_turns_summary": latest_turns_summary,
                "is_heartbeat": is_heartbeat,
            }
        )

    active_multi_turn = []
    active_zero_turn = []
    for item in normalized:
        if item["status"] in ACTIVE_STATUSES or item["status"].startswith("active"):
            if item["turn_count"] > 1:
                active_multi_turn.append(item)
            elif item["turn_count"] == 0:
                active_zero_turn.append(item)
            continue
        if item["turn_count"] <= 1:
            continue
        if item["status"] in {"unknown", "-", ""}:
            active_multi_turn.append(item)

    preferred = [item for item in active_multi_turn if not item["is_heartbeat"]]
    active_zero_turn_non_heartbeat = [item for item in active_zero_turn if not item["is_heartbeat"]]
    non_heartbeat = [item for item in normalized if item["turn_count"] > 0 and not item["is_heartbeat"]]

    if preferred:
        chosen = max(preferred, key=lambda item: _sort_key(item["raw"]))
        return {
            "state": "active",
            "instance_label": f"openclaw-{instance}",
            "port": port,
            "session_id": chosen["session_id"],
            "target": chosen["target"],
            "status": chosen["status"],
            "updated_at": chosen["updated_at"],
            "turn_count": chosen["turn_count"],
            "latest_turns_summary": chosen["latest_turns_summary"],
        }

    if active_zero_turn_non_heartbeat:
        chosen = max(active_zero_turn_non_heartbeat, key=lambda item: _sort_key(item["raw"]))
        return {
            "state": "active",
            "instance_label": f"openclaw-{instance}",
            "port": port,
            "session_id": chosen["session_id"],
            "target": chosen["target"],
            "status": chosen["status"],
            "updated_at": chosen["updated_at"],
            "turn_count": chosen["turn_count"],
            "latest_turns_summary": chosen["latest_turns_summary"],
        }

    if non_heartbeat:
        chosen = max(non_heartbeat, key=lambda item: _sort_key(item["raw"]))
        return {
            "state": "recent",
            "instance_label": f"openclaw-{instance}",
            "port": port,
            "session_id": chosen["session_id"],
            "target": chosen["target"],
            "status": chosen["status"],
            "updated_at": chosen["updated_at"],
            "turn_count": chosen["turn_count"],
            "latest_turns_summary": chosen["latest_turns_summary"],
        }

    if active_multi_turn:
        return {
            "state": "idle",
            "instance_label": f"openclaw-{instance}",
            "port": port,
            "session_id": "-",
            "target": "-",
            "status": "idle",
            "updated_at": "-",
            "turn_count": 0,
            "latest_turns_summary": "No active non-heartbeat multi-turn session.",
        }

    return {
        "state": "idle",
        "instance_label": f"openclaw-{instance}",
        "port": port,
        "session_id": "-",
        "target": "-",
        "status": "idle",
        "updated_at": "-",
        "turn_count": 0,
        "latest_turns_summary": "No active multi-turn session.",
    }


def _truncate(text: str, width: int) -> str:
    """Truncate *text* to *width* characters, appending ``'...'`` if shortened."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def render_summary(summary: dict[str, Any], lines: int | None = None, columns: int | None = None) -> str:
    """Render a session summary as a compact, terminal-friendly multi-line string."""
    max_lines = max(lines or int(os.environ.get("LINES", "24")), 3)
    max_columns = max(columns or int(os.environ.get("COLUMNS", "80")), 20)
    base_lines = [
        _truncate(
            f"{summary['instance_label']} :{summary['port']} "
            f"state={summary['state']} turns={summary['turn_count']}",
            max_columns,
        ),
        _truncate(f"session={summary['session_id']} status={summary['status']}", max_columns),
        _truncate(f"target={summary['target']}", max_columns),
        _truncate(f"summary: {summary['latest_turns_summary']}", max_columns),
    ]
    if max_lines >= 4:
        rendered = base_lines
    else:
        rendered = base_lines[:2] + [base_lines[-1]]
    return "\n".join(rendered[:max_lines])


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for standalone usage."""
    parser = argparse.ArgumentParser(description="Render OpenClaw session summaries.")
    parser.add_argument("--instance", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--container-name")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    """CLI entrypoint: read session JSON from stdin, emit summary as JSON or pretty text."""
    parser = _build_arg_parser()
    args = parser.parse_args()
    summary = summarize_instance(
        instance=args.instance,
        port=args.port,
        store_root=args.store_root,
        container_name=args.container_name,
    )
    if args.pretty:
        print(render_summary(summary))
    else:
        json.dump(summary, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
