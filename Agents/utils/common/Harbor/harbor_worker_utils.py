#!/usr/bin/env python3
"""Small helpers for run_harbor_worker.sh."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path


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
