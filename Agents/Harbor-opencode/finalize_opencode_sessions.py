#!/usr/bin/env python3
"""Finalize opencode realtime traces after the opencode CLI exits.

The opencode plugin does not reliably emit a terminal session event in Harbor
task containers. The realtime hook still records the active session id in its
state file, so the Harbor agent wrapper runs this small finalizer immediately
after `opencode run` returns to close every unfinished opencode-owned trace.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _resolve_timeout_hook() -> Path:
    """Locate the opencode realtime hook for host-side timeout replay."""
    repo_root = Path(__file__).resolve().parents[1]
    source_dir = Path(
        os.environ.get("TRACE_PLUGIN_SOURCE_DIR", repo_root / "third_party" / "agent-opik-plugin")
    ).expanduser()
    candidates = [
        Path(
            os.environ.get(
                "TRACE_PLUGIN_OPENCODE_HOOK_SOURCE",
                source_dir
                / "src"
                / "sii_opik_plugin"
                / "opencode"
                / "opencode_realtime_trace.py",
            )
        ).expanduser(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _fallback_timeout_trace(logs_dir: Path, status: str) -> int:
    if status != "timeout":
        return 0
    # Harbor versions differ on what the outer worker can recover after a
    # timeout: sometimes it passes the trial dir, sometimes the agent log dir.
    agent_log = logs_dir / "agent" / "opencode.txt"
    if not agent_log.exists():
        agent_log = logs_dir / "opencode.txt"
    if not agent_log.exists():
        return 0
    session_id = ""
    first_text = ""
    last_text = ""
    try:
        for line in agent_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            session_id = session_id or str(event.get("sessionID") or "")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                first_text = first_text or text.strip()
                last_text = text.strip()
    except Exception as exc:
        print(f"[WARN] unable to parse opencode log fallback: {exc}", file=sys.stderr)
        return 1
    if not session_id:
        return 0
    try:
        from opik import Opik
    except Exception as exc:
        print(f"[WARN] unable to import opik for fallback timeout trace: {exc}", file=sys.stderr)
        return 1
    try:
        from uuid6 import uuid7
    except Exception as exc:
        print(f"[WARN] unable to import uuid6 for fallback timeout trace: {exc}", file=sys.stderr)
        return 1

    project_name = os.environ.get("OPIK_PROJECT_NAME") or os.environ.get("OC_OPIK_PROJECT") or "opencode-realtime"
    trace_id = str(uuid7())
    task_root = logs_dir.parent if logs_dir.name == "agent" else logs_dir
    task_name = os.environ.get("TB_TASK_ID") or task_root.name.split("__", 1)[0]
    now = datetime.now(timezone.utc)
    payload = {
        "id": trace_id,
        "project_name": project_name,
        "name": task_name,
        "start_time": now,
        "end_time": now,
        "input": first_text or session_id,
        "output": {"status": "timeout"},
        "metadata": {
            "thread_id": trace_id,
            "thread_name": task_name,
            "first_message": first_text,
            "final_status": "timeout",
            "source": "harbor_opencode_timeout_fallback",
            "session_id": session_id,
        },
        "tags": ["timeout", "opencode", "harbor"],
        "thread_id": trace_id,
    }
    try:
        client = Opik(project_name=project_name)
        try:
            client.rest_client.traces.create_trace(**payload)
        except Exception:
            client.rest_client.traces.update_trace(trace_id, **{k: v for k, v in payload.items() if k not in {"id", "start_time"}})
        try:
            client.flush()
            client.shutdown()
        except Exception:
            pass
        print(f"[INFO] fallback timeout trace finalized trace_id={trace_id}")
        return 0
    except Exception as exc:
        print(f"[WARN] fallback timeout trace failed: {exc}", file=sys.stderr)
        return 1


def _trace_to_opik_enabled() -> bool:
    if os.environ.get("OPIK_TRACK_DISABLE", "").lower() in {"true", "1"}:
        return False
    return os.environ.get("TRACE_TO_OPIK", "true") not in {"false", "0"}


def main() -> int:
    # Defense in depth for the worker-side timeout replay: even if a caller
    # forgets the shell gate, a trace-off run must never write to Opik.
    if not _trace_to_opik_enabled():
        print("[INFO] finalize skipped: Opik tracing disabled")
        return 0

    status = "completed"
    logs_dir: Path | None = None
    argv = sys.argv[1:]
    if "--status" in argv:
        idx = argv.index("--status")
        if idx + 1 < len(argv):
            status = argv[idx + 1]
    if "--logs-dir" in argv:
        idx = argv.index("--logs-dir")
        if idx + 1 < len(argv):
            logs_dir = Path(argv[idx + 1]).resolve()

    home = Path.home()
    if logs_dir is not None:
        state_file = logs_dir / "opencode-runtime-state.json"
        hook = _resolve_timeout_hook()
    else:
        state_file = home / ".opencode" / "state" / "opik_realtime_state.json"
        hook = home / ".config" / "opencode" / "plugins" / "opencode_realtime_trace.py"

    if not state_file.exists() or not hook.exists():
        if logs_dir is not None:
            return _fallback_timeout_trace(logs_dir, status)
        return 0

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] unable to read opencode realtime state: {exc}", file=sys.stderr)
        return 0

    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return 0

    rc = 0
    for session_id, session in sessions.items():
        if not isinstance(session, dict):
            continue
        if session.get("trace_finalized"):
            continue
        if session.get("trace_owner") not in (None, "", "opencode"):
            continue

        payload = {
            "event": "session_end",
            "session_id": session_id,
            "event_timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": status,
            "source": "harbor_opencode_finalizer",
        }
        env = os.environ.copy()
        if logs_dir is not None:
            storage_dir = logs_dir / "opencode-runtime-storage"
            db_file = logs_dir / "opencode-runtime.db"
            env["OC_OPIK_STATE_FILE"] = str(state_file)
            env["OC_OPIK_LOCK_FILE"] = str(logs_dir / "opencode-runtime-state.lock")
            env["OC_OPIK_LOG_FILE"] = str(logs_dir / "opencode-runtime-hook.log")
            env["OC_OPIK_LOGS_DIR"] = str(logs_dir)
            if storage_dir.exists():
                payload["storage_path"] = str(storage_dir)
            elif db_file.exists():
                payload["db_path"] = str(db_file)
        try:
            result = subprocess.run(
                [sys.executable, str(hook), "session_end"],
                input=json.dumps(payload),
                text=True,
                env=env,
                timeout=float(os.environ.get("OC_OPIK_FINALIZE_TIMEOUT_S", "90")),
                check=False,
            )
            if result.returncode != 0:
                rc = result.returncode
        except Exception as exc:
            print(f"[WARN] unable to finalize opencode session {session_id}: {exc}", file=sys.stderr)
            rc = 1

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
