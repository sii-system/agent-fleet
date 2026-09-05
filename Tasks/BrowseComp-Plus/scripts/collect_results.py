#!/usr/bin/env python3
"""Convert Harbor trial artifacts to the official BrowseComp run schema."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from common import atomic_write_json, validate_task_id

DOCID_RE = re.compile(r'"docid"\s*:\s*"([^"]+)"')


def walk(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def text_blocks(value: object) -> list[str]:
    texts: list[str] = []
    for item in walk(value):
        if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            text = item.get("text", item.get("output"))
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def collect_docids(value: object, output: set[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("docid"), (str, int)):
            output.add(str(value["docid"]))
        for child in value.values():
            collect_docids(child, output)
    elif isinstance(value, list):
        for child in value:
            collect_docids(child, output)
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(("{", "[")):
            try:
                collect_docids(json.loads(candidate), output)
            except json.JSONDecodeError:
                pass
        for docid in DOCID_RE.findall(value):
            output.add(docid)


def parse_agent_log(path: Path) -> tuple[str, Counter[str], set[str]]:
    final = ""
    counts: Counter[str] = Counter()
    docids: set[str] = set()
    seen_calls: set[str] = set()
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return final, counts, docids
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") in {"message_end", "turn_end"}:
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                candidates = text_blocks(message.get("content"))
                if candidates:
                    final = "\n".join(candidates)
        elif isinstance(event, dict) and event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                final = part["text"].strip()
        for item in walk(event):
            if isinstance(item, dict) and item.get("role") == "assistant":
                candidates = text_blocks(item.get("content"))
                if candidates:
                    final = "\n".join(candidates)
        for item in walk(event):
            if not isinstance(item, dict):
                continue
            name = item.get("name", item.get("toolName", item.get("tool", item.get("function_name"))))
            short_name = str(name or "").rsplit("__", 1)[-1]
            if short_name not in {"search", "get_document"}:
                continue
            call_id = str(item.get("id", item.get("toolCallId", item.get("callID", item.get("tool_call_id", "")))))
            marker = call_id or f"{path}:{len(seen_calls)}:{short_name}"
            event_type = str(item.get("type", ""))
            if marker not in seen_calls and event_type not in {"tool_execution_update", "tool_execution_end"}:
                counts[short_name] += 1
                seen_calls.add(marker)
        collect_docids(event, docids)
    return final, counts, docids


def parse_events(path: Path) -> tuple[Counter[str], set[str]]:
    counts: Counter[str] = Counter()
    docids: set[str] = set()
    if not path.is_file():
        return counts, docids
    for line in path.read_text(errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool = event.get("tool") if isinstance(event, dict) else None
        if tool in {"search", "get_document"}:
            counts[str(tool)] += 1
        values = event.get("docids", []) if isinstance(event, dict) else []
        if isinstance(values, list):
            docids.update(str(value) for value in values)
    return counts, docids


def trial_task_id(result: dict[str, Any], trial_dir: Path) -> str:
    for key in ("task_name", "task_id"):
        if result.get(key):
            return validate_task_id(result[key])
    return validate_task_id(trial_dir.name.split("__", 1)[0])


def collect_trial(result_path: Path) -> dict[str, object]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise TypeError(f"Harbor result is not an object: {result_path}")
    trial_dir = result_path.parent
    task_id = trial_task_id(result, trial_dir)
    answer = ""
    fallback_answer = ""
    counts: Counter[str] = Counter()
    docids: set[str] = set()
    agent_dir = trial_dir / "agent"
    fallback_answer = "\n".join(text_blocks(result.get("agent_result")))
    log_paths = []
    if agent_dir.is_dir():
        log_paths = sorted(agent_dir.rglob("*.txt")) + [
            path for path in sorted(agent_dir.rglob("*.jsonl")) if path.name != "browsecomp-events.jsonl"
        ]
    for log_path in log_paths:
        candidate, log_counts, log_docids = parse_agent_log(log_path)
        if candidate:
            answer = candidate
        counts.update(log_counts)
        docids.update(log_docids)
    event_counts, event_docids = parse_events(agent_dir / "browsecomp-events.jsonl")
    if event_counts:
        counts = event_counts
    docids.update(event_docids)
    answer = answer or fallback_answer
    exception = result.get("exception_info")
    completed = bool(answer.strip()) and not exception
    agent_result = result.get("agent_result")
    agent_metadata = agent_result.get("metadata") if isinstance(agent_result, dict) else None
    agent_info = result.get("agent_info")
    model_info = agent_info.get("model_info") if isinstance(agent_info, dict) else None
    config = result.get("config")
    config_agent = config.get("agent") if isinstance(config, dict) else None
    metadata = {
        "harbor_result": str(result_path),
        "trial_name": result.get("trial_name", trial_dir.name),
        "agent": result.get("agent_name", result.get("agent"))
        or (agent_info.get("name") if isinstance(agent_info, dict) else None),
        "model": (agent_metadata.get("model") if isinstance(agent_metadata, dict) else None)
        or (model_info.get("name") if isinstance(model_info, dict) else None)
        or (config_agent.get("model_name") if isinstance(config_agent, dict) else None),
    }
    return {
        "query_id": task_id,
        "tool_call_counts": dict(counts),
        "status": "completed" if completed else "failed",
        "retrieved_docids": sorted(docids),
        "result": [{"type": "output_text", "output": answer}],
        "metadata": metadata,
    }


def load_manifest_task_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise TypeError(f"task manifest has no tasks list: {path}")
    task_ids: list[str] = []
    seen: set[str] = set()
    for task in payload["tasks"]:
        if not isinstance(task, dict):
            raise TypeError(f"task manifest contains a non-object task: {path}")
        task_id = validate_task_id(task.get("query_id"))
        if task_id in seen:
            raise ValueError(f"task manifest contains duplicate query_id {task_id}")
        seen.add(task_id)
        task_ids.append(task_id)
    if not task_ids:
        raise ValueError(f"task manifest contains no tasks: {path}")
    if payload.get("task_count") != len(task_ids):
        raise ValueError(
            f"task manifest count mismatch: expected {payload.get('task_count')}, found {len(task_ids)}"
        )
    return task_ids


def failed_result(
    task_id: str, reason: str, result_path: Path | None = None
) -> dict[str, object]:
    metadata: dict[str, object] = {"collection_error": reason}
    if result_path is not None:
        metadata["harbor_result"] = str(result_path)
    return {
        "query_id": validate_task_id(task_id),
        "tool_call_counts": {},
        "status": "failed",
        "retrieved_docids": [],
        "result": [{"type": "output_text", "output": ""}],
        "metadata": metadata,
    }


def discover_results(
    jobs_root: Path, expected_task_ids: Iterable[str] | None = None
) -> dict[str, Path]:
    expected = (
        {validate_task_id(task_id) for task_id in expected_task_ids}
        if expected_task_ids is not None
        else None
    )
    newest: dict[str, tuple[int, Path]] = {}
    for path in jobs_root.rglob("result.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError(f"Harbor result is not an object: {path}")
            task_id = trial_task_id(payload, path.parent)
            modified = path.stat().st_mtime_ns
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if expected is not None and task_id not in expected:
            continue
        if task_id not in newest or modified > newest[task_id][0]:
            newest[task_id] = (modified, path)
    return {task_id: value[1] for task_id, value in newest.items()}


def collect(
    jobs_root: Path,
    output_dir: Path,
    task_id: str = "",
    task_manifest: Path | None = None,
) -> list[dict[str, object]]:
    reconcile_manifest = task_manifest is not None
    if task_id and task_manifest is not None:
        raise ValueError("--task-id and --task-manifest cannot be used together")
    if task_id:
        candidate = jobs_root / "result.json" if jobs_root.is_dir() else jobs_root
        paths = {validate_task_id(task_id): candidate}
    elif task_manifest is not None:
        expected_ids = load_manifest_task_ids(task_manifest)
        discovered = discover_results(jobs_root, expected_ids)
        paths = {expected_id: discovered.get(expected_id) for expected_id in expected_ids}
    else:
        paths = discover_results(jobs_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    if reconcile_manifest:
        for stale_path in output_dir.glob("*.json"):
            if stale_path.stem not in paths:
                stale_path.unlink()
    collected = []
    for expected_id, result_path in sorted(paths.items()):
        if result_path is None:
            item = failed_result(expected_id, "missing Harbor result.json")
        else:
            try:
                item = collect_trial(result_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                if not reconcile_manifest:
                    raise
                item = failed_result(
                    expected_id,
                    f"could not collect Harbor result: {type(exc).__name__}: {exc}",
                    result_path,
                )
        if str(item["query_id"]) != expected_id:
            raise ValueError(f"task mismatch: expected {expected_id}, found {item['query_id']}")
        atomic_write_json(output_dir / f"{expected_id}.json", item)
        collected.append(item)
    return collected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--task-manifest", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    collected = collect(
        args.jobs_root.resolve(),
        args.output_dir.resolve(),
        args.task_id,
        args.task_manifest.resolve() if args.task_manifest else None,
    )
    payload = {"count": len(collected), "completed": sum(item["status"] == "completed" for item in collected), "output_dir": str(args.output_dir.resolve())}
    if args.json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(f"Collected {payload['count']} BrowseComp results ({payload['completed']} completed) at {payload['output_dir']}")
    return 0 if collected else 1


if __name__ == "__main__":
    raise SystemExit(main())
