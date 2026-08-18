#!/usr/bin/env python3
"""Small helpers for run_harbor_worker.sh."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    """Read a positive float env var, falling back to a default."""
    raw = os.environ.get(name, "")
    try:
        if not raw:
            return default
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def latest_result(root: Path) -> int:
    # Harbor also writes job-level result.json files. Prefer trial-level files
    # that carry verifier_result or is_resolved fields.
    for path in sorted(root.rglob("result.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and (
            "verifier_result" in data or "is_resolved" in data
        ):
            print(path)
            return 0
    return 1


def summarize_result(result_file: Path) -> int:
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1

    reward = None
    verifier = data.get("verifier_result") or {}
    if isinstance(verifier, dict):
        rewards = verifier.get("rewards") or {}
        if isinstance(rewards, dict):
            reward = rewards.get("reward")
        if reward is None and "is_resolved" in verifier:
            reward = 1.0 if verifier.get("is_resolved") else 0.0
    if reward is None and "is_resolved" in data:
        reward = 1.0 if data.get("is_resolved") else 0.0

    exc = data.get("exception_info") or {}
    exc_type = exc.get("exception_type") if isinstance(exc, dict) else None
    print("" if reward is None else reward)
    print(exc_type or "")
    return 0


def _clean(value: object, limit: int = 500) -> str:
    text = " ".join(str(value).replace("\n", " ").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def stream_claude_log(task_root: Path) -> int:
    log = None
    while log is None:
        matches = sorted(
            task_root.rglob("agent/claude-code.txt"), key=lambda p: p.stat().st_mtime
        )
        if matches:
            log = matches[-1]
            break
        time.sleep(1)

    with log.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            typ = event.get("type")
            msg = event.get("message") or {}
            if typ == "assistant":
                for item in msg.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and item.get("text"):
                        print(f"[llm] {_clean(item.get('text'))}", flush=True)
                    elif item.get("type") == "tool_use":
                        name = item.get("name") or "tool"
                        inp = item.get("input") or {}
                        detail = inp.get("command") or inp.get("file_path") or inp if isinstance(inp, dict) else inp
                        print(f"[tool] {name}: {_clean(detail)}", flush=True)
            elif typ == "user":
                for item in msg.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        print(
                            f"[tool_result] {_clean(item.get('content', ''))}",
                            flush=True,
                        )
            elif typ == "result" and event.get("result"):
                print(f"[result] {_clean(event.get('result'))}", flush=True)


def stream_opencode_log(task_root: Path) -> int:
    log = None
    while log is None:
        matches = sorted(
            task_root.rglob("agent/opencode.txt"), key=lambda p: p.stat().st_mtime
        )
        if matches:
            log = matches[-1]
            break
        time.sleep(1)

    with log.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            ptype = part.get("type")
            if ptype in {"text", "reasoning"} and part.get("text"):
                print(f"[llm] {_clean(part.get('text'))}", flush=True)
            elif ptype == "tool":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                name = part.get("tool") or state.get("tool") or "tool"
                status = state.get("status") or part.get("status") or ""
                inp = state.get("input") or part.get("input") or {}
                out = state.get("output") or part.get("output")
                detail = inp.get("command") or inp.get("file_path") or inp if isinstance(inp, dict) else inp
                suffix = f" {status}" if status else ""
                if detail:
                    print(f"[tool] {name}{suffix}: {_clean(detail)}", flush=True)
                else:
                    print(f"[tool] {name}{suffix}", flush=True)
                if out:
                    print(f"[tool_result] {_clean(out)}", flush=True)
            elif event.get("type") in {"result", "error"}:
                value = event.get("result") or event.get("error") or event.get("message")
                if value:
                    print(f"[result] {_clean(value)}", flush=True)


def stream_pi_log(task_root: Path) -> int:
    log = None
    wait_seconds = _float_env("PI_STREAM_WAIT_SECONDS", 1200.0)
    hard_seconds = _float_env("PI_STREAM_MAX_WAIT_SECONDS", 12 * 60 * 60.0)
    if wait_seconds < 1:
        wait_seconds = 1.0
    hard_seconds = max(hard_seconds, wait_seconds)
    warn_deadline = time.monotonic() + wait_seconds
    hard_deadline = time.monotonic() + hard_seconds
    warned = False
    last_scan = 0.0
    while log is None:
        direct = task_root / "agent" / "pi.txt"
        if direct.is_file():
            log = direct
            break
        # The common case is the directly-mounted agent/pi.txt. Only walk
        # the whole task tree periodically as a fallback for nested trial
        # roots, so big job dirs are not re-scanned every second.
        now = time.monotonic()
        if now - last_scan >= 30.0:
            matches = sorted(
                task_root.rglob("agent/pi.txt"), key=lambda p: p.stat().st_mtime
            )
            if matches:
                log = matches[-1]
                break
            last_scan = now
        if not warned and now >= warn_deadline:
            print(
                "[WARN] agent/pi.txt still not present after "
                f"{wait_seconds:g}s (PI_STREAM_WAIT_SECONDS); pi install "
                "may just be slow extracting the bundled Node/runtime "
                "archives, keeping the tailer alive",
                file=sys.stderr,
            )
            warned = True
        if now >= hard_deadline:
            print(
                "[ERROR] agent/pi.txt never appeared under "
                f"{task_root} within {hard_seconds:g}s "
                "(PI_STREAM_MAX_WAIT_SECONDS); pi setup truly failed",
                file=sys.stderr,
            )
            return 1
        time.sleep(1)

    with log.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            message = event.get("message")
            if event_type == "message_end" and isinstance(message, dict):
                if message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    print(f"[llm] {_clean(content)}", flush=True)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") in {"text", "output_text"} and block.get("text"):
                            print(f"[llm] {_clean(block['text'])}", flush=True)
            elif event_type == "tool_execution_start":
                name = event.get("toolName") or event.get("name") or "tool"
                args = event.get("args") or event.get("input") or {}
                print(f"[tool] {name}: {_clean(args)}", flush=True)
            elif event_type == "tool_execution_end":
                result = event.get("result") or event.get("output")
                if result:
                    print(f"[tool_result] {_clean(result)}", flush=True)


def prepare_claude_timeout_backup(logs_dir: Path, project_name: str) -> int:
    backup_state = logs_dir / "opik-runtime-state.json"
    backup_transcript = logs_dir / "opik-runtime-transcript.jsonl"
    if backup_state.exists():
        return 0

    state_file = logs_dir / "agent" / "sessions" / "state" / "opik_hook_state.json"
    projects_dir = logs_dir / "agent" / "sessions" / "projects"
    transcripts = sorted(projects_dir.rglob("*.jsonl")) if projects_dir.exists() else []
    if not state_file.exists() or not transcripts:
        return 1

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    if not isinstance(state, dict) or not state:
        return 1

    key = next(iter(state))
    transcript = transcripts[0]
    payload = {
        "key": key,
        "session_id": transcript.stem,
        "transcript_path": str(transcript),
        "project_name": project_name,
        "state": state.get(key, {}),
        "backup_state_path": str(backup_state),
        "backup_transcript_path": str(backup_transcript),
    }
    backup_state.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    shutil.copy2(transcript, backup_transcript)
    return 0


def online_early_stop_reason(events_path: Path, task_id: int) -> str | None:
    try:
        handle = events_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return None

    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("task_id") != task_id or event.get("task_blocking") is not True:
                continue
            name = str(event.get("event") or "task-blocking")
            return f"OnlineAnalysisEarlyStop:{name}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "latest-result",
            "summarize-result",
            "stream-claude-log",
            "stream-opencode-log",
            "stream-pi-log",
            "prepare-claude-timeout-backup",
            "online-early-stop-reason",
        ),
    )
    parser.add_argument("path")
    parser.add_argument("--project-name", default="")
    parser.add_argument("--task-id", type=int)
    args = parser.parse_args()
    path = Path(args.path)

    if args.command == "latest-result":
        return latest_result(path)
    if args.command == "summarize-result":
        return summarize_result(path)
    if args.command == "prepare-claude-timeout-backup":
        return prepare_claude_timeout_backup(path, args.project_name)
    if args.command == "stream-opencode-log":
        return stream_opencode_log(path)
    if args.command == "stream-pi-log":
        return stream_pi_log(path)
    if args.command == "online-early-stop-reason":
        if args.task_id is None:
            parser.error("--task-id is required for online-early-stop-reason")
        reason = online_early_stop_reason(path, args.task_id)
        if reason is None:
            return 1
        print(reason)
        return 0
    return stream_claude_log(path)


if __name__ == "__main__":
    raise SystemExit(main())
