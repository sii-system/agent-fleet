#!/usr/bin/env python3
"""Rollout-only worker maintenance helpers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
from collections import Counter
from pathlib import Path

HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
RESERVED_HEADERS = {"x-session-id", "proxy-x-session-id"}
SUCCESS_VALUES = {"1", "1.0", "true", "success", "resolved", "pass", "passed"}


def _format_json_value(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _json_path_value(payload: object, path: str) -> object:
    value = payload
    for part in path.split("."):
        if part:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
    return value


def json_path(payload: object, path: str) -> str:
    return _format_json_value(_json_path_value(payload, path))


def first_json_path(payload: object, paths: list[str]) -> str:
    for path in paths:
        value = json_path(payload, path)
        if value:
            return value
    return ""


def build_llm_kwargs(values: list[str], headers_json: str) -> str:
    names = ("temperature", "top_p", "top_k", "min_p", "timeout", "max_retries")
    integer_names = {"top_k", "max_retries"}
    payload: dict[str, object] = {}
    if len(values) != len(names):
        raise ValueError(f"expected {len(names)} LLM values, received {len(values)}")
    for name, raw in zip(names, values):
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            payload[name] = int(raw) if name in integer_names else float(raw)
        except ValueError:
            payload[name] = raw
    headers = json.loads(headers_json)
    if headers:
        payload["extra_headers"] = headers
    return json.dumps(payload, separators=(",", ":"))


def build_result(
    request_file: Path,
    result_file: Path | None,
    console_log: str,
    reward: str,
    exception_type: str,
    exit_code: int,
    result_out: Path,
    status: str,
    benchmark_result_file: Path | None = None,
) -> None:
    request = json.loads(request_file.read_text(encoding="utf-8"))
    result_data: object = {}
    if result_file:
        try:
            result_data = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result_data = {}
    agent_result = result_data.get("agent_result") if isinstance(result_data, dict) else None
    verifier_result = result_data.get("verifier_result") if isinstance(result_data, dict) else None
    exception_info = result_data.get("exception_info") if isinstance(result_data, dict) else None
    if not isinstance(exception_info, dict) and exception_type:
        exception_info = {"exception_type": exception_type}
    harbor_reward = float(reward) if reward.strip() not in {"", "None"} else None
    benchmark_result: object = None
    benchmark_reward: float | None = None
    if benchmark_result_file:
        try:
            benchmark_result = json.loads(benchmark_result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            benchmark_result = None
        if isinstance(benchmark_result, dict):
            candidate = benchmark_result.get("reward")
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                benchmark_reward = float(candidate)
    payload = {
        "ok": status == "completed" and not exception_type,
        "status": status,
        "task_id": request.get("task_id"),
        "task_path": request.get("task_path"),
        "ray_submission_id": request.get("ray_submission_id"),
        "polar_task_id": request.get("polar_task_id"),
        "display_name": request.get("display_name"),
        "environment_type": request.get("environment_type"),
        "trial_name": result_file.parent.name if result_file else "",
        "trial_uri": str(result_file.parent) if result_file else "",
        "reward": benchmark_reward if benchmark_reward is not None else harbor_reward,
        "harbor_reward": harbor_reward,
        "benchmark_result": benchmark_result,
        "rollout_details": agent_result.get("rollout_details") if isinstance(agent_result, dict) else None,
        "num_turns": (agent_result.get("metadata") or {}).get("n_episodes") if isinstance(agent_result, dict) else None,
        "agent_result": agent_result,
        "verifier_result": verifier_result,
        "exception_info": exception_info,
        "metadata": {
            "request_id": request.get("request_id"),
            "session_id": request.get("session_id"),
            "ray_submission_id": request.get("ray_submission_id"),
            "polar_task_id": request.get("polar_task_id"),
            "display_name": request.get("display_name"),
            "console_log": console_log,
            "exit_code": exit_code,
        },
    }
    temporary = result_out.with_name(f".{result_out.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(result_out)


def read_benchmark_reward(path: Path, expected_benchmark: str = "") -> float:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("benchmark result must be a JSON object")
    if expected_benchmark and payload.get("benchmark") != expected_benchmark:
        raise ValueError(
            f"benchmark result mismatch: expected {expected_benchmark!r}, got {payload.get('benchmark')!r}"
        )
    reward = payload.get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        raise TypeError("benchmark result reward must be numeric")
    normalized = float(reward)
    if not math.isfinite(normalized):
        raise ValueError("benchmark result reward must be finite")
    return normalized


def render_result_stats(results_dir: Path) -> str:
    finished = trace_success = task_success = 0
    exceptions: Counter[str] = Counter()
    rewards: Counter[str] = Counter()
    if results_dir.exists():
        for path in sorted(results_dir.glob("*.json")):
            try:
                item = json.loads(path.read_text(errors="ignore"))
            except json.JSONDecodeError:
                continue
            finished += 1
            trace_success += bool(item.get("ok"))
            reward = item.get("reward")
            task_success += str(reward or "").strip().lower() in SUCCESS_VALUES
            rewards["none" if reward is None else str(reward)] += 1
            exception = item.get("exception_info") or {}
            name = exception.get("exception_type") if isinstance(exception, dict) else str(exception)
            if name:
                exceptions[str(name)] += 1
    lines = [
        f"finished:     {finished}",
        f"gen_trace_success: {trace_success}",
        f"gen_trace_fail:    {finished - trace_success}",
        "",
        "reward stats:",
        f"task_success_rate: {(task_success / finished * 100.0) if finished else 0.0:.2f}%",
    ]
    lines.extend(
        [f"reward={reward}: {count}" for reward, count in sorted(rewards.items(), key=lambda item: (item[0] != "1.0", item[0]))]
        or ["(none)"]
    )
    lines.extend(["", "exception stats:"])
    lines.extend([f"{name}: {count}" for name, count in exceptions.most_common(10)] or ["(none)"])
    return "\n".join(lines)


def render_recent_results(results_dir: Path) -> str:
    items: list[tuple[object, object, object, object]] = []
    if results_dir.exists():
        for path in sorted(results_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)[-8:]:
            try:
                item = json.loads(path.read_text(errors="ignore"))
            except json.JSONDecodeError:
                continue
            exception = item.get("exception_info") or {}
            if isinstance(exception, dict):
                exc = exception.get("exception_type") or "none"
            else:
                exc = str(exception or "none")
            items.append(
                (
                    item.get("display_name") or item.get("task_id") or path.stem,
                    item.get("status") or ("completed" if item.get("ok") else "failed"),
                    "none" if item.get("reward") is None else item.get("reward"),
                    exc,
                )
            )
    if not items:
        return "(none)"
    return "\n".join(
        f"- {display} status={status} reward={reward} exception={exc}"
        for display, status, reward, exc in items
    )


def build_request_headers(raw: str, session_id: str = "") -> dict[str, str]:
    """Build trusted model-request headers from the versioned host config."""
    if not raw.strip():
        config: object = {"version": 1}
    else:
        config = json.loads(raw)
    if not isinstance(config, dict):
        raise TypeError("MODEL_REQUEST_CONFIG_JSON must be a JSON object")
    unknown = set(config) - {"version", "headers"}
    if unknown:
        raise ValueError(f"unsupported model request config fields: {sorted(unknown)}")
    if config.get("version") != 1:
        raise ValueError("MODEL_REQUEST_CONFIG_JSON version must be 1")

    header_config = config.get("headers", {})
    if not isinstance(header_config, dict):
        raise TypeError("model request config headers must be an object")
    unknown = set(header_config) - {"set"}
    if unknown:
        raise ValueError(f"unsupported header operations: {sorted(unknown)}")
    configured = header_config.get("set", {})
    if not isinstance(configured, dict):
        raise TypeError("model request config headers.set must be an object")

    headers: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in configured.items():
        if not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid model request header name: {name!r}")
        normalized_name = name.lower()
        if normalized_name in RESERVED_HEADERS:
            raise ValueError(f"model request header is reserved: {name!r}")
        if normalized_name in seen:
            raise ValueError(f"duplicate case-insensitive header: {name!r}")
        if not isinstance(value, str):
            raise TypeError(f"model request header {name!r} must be a string")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"model request header {name!r} contains control characters")
        seen.add(normalized_name)
        headers[name] = value

    if session_id:
        headers.update(
            {
                "X-Session-Id": session_id,
                "Proxy-X-Session-Id": session_id,
            }
        )
    return headers


def render_header_lines(existing: str, headers: dict[str, str]) -> str:
    """Merge headers into a newline-separated client header setting."""
    replaced = {name.lower() for name in headers}
    lines = [
        line
        for line in existing.splitlines()
        if line.strip()
        and line.partition(":")[0].strip().lower() not in replaced
    ]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    return "\n".join(lines)


def prune_trial_artifacts(worker_root: Path, keep: int) -> None:
    """Keep only the newest rollout trial directories for one worker."""
    keep = max(1, keep)
    try:
        trials = [path for path in worker_root.iterdir() if path.is_dir()]
    except FileNotFoundError:
        return
    trials.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in trials[keep:]:
        shutil.rmtree(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "prune-trials",
            "request-headers",
            "render-header-lines",
            "json-get",
            "json-get-first",
            "build-result",
            "build-llm-kwargs",
            "benchmark-reward",
            "result-stats",
            "recent-results",
        ),
    )
    parser.add_argument("arguments", nargs="*")
    parser.add_argument("--keep", type=int, default=20)
    args = parser.parse_args()

    if args.command == "request-headers":
        headers = build_request_headers(
            os.environ.get("MODEL_REQUEST_CONFIG_JSON", ""),
            os.environ.get("MODEL_REQUEST_SESSION_ID", ""),
        )
        print(json.dumps(headers, separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "render-header-lines":
        headers = json.loads(os.environ.get("MODEL_REQUEST_HEADERS_JSON", "{}"))
        print(
            render_header_lines(
                os.environ.get("HARBOR_ANTHROPIC_CUSTOM_HEADERS", ""), headers
            )
        )
        return 0
    if args.command == "json-get":
        payload = json.loads(Path(args.arguments[0]).read_text(encoding="utf-8"))
        value = _json_path_value(payload, args.arguments[1])
        print("" if value is None else value)
        return 0
    if args.command == "json-get-first":
        payload = json.loads(Path(args.arguments[0]).read_text(encoding="utf-8"))
        print(first_json_path(payload, args.arguments[1:]))
        return 0
    if args.command == "build-result":
        result_file = Path(args.arguments[1]) if args.arguments[1] else None
        benchmark_result_file = Path(args.arguments[8]) if len(args.arguments) > 8 and args.arguments[8] else None
        build_result(
            Path(args.arguments[0]),
            result_file,
            args.arguments[2],
            args.arguments[3],
            args.arguments[4],
            int(args.arguments[5]),
            Path(args.arguments[6]),
            args.arguments[7],
            benchmark_result_file,
        )
        return 0
    if args.command == "benchmark-reward":
        print(read_benchmark_reward(Path(args.arguments[0]), os.environ.get("RL_BENCHMARK", "")))
        return 0
    if args.command == "build-llm-kwargs":
        print(
            build_llm_kwargs(
                args.arguments,
                os.environ.get("MODEL_REQUEST_HEADERS_JSON", "{}"),
            )
        )
        return 0
    if args.command == "result-stats":
        print(render_result_stats(Path(args.arguments[0])))
        return 0
    if args.command == "recent-results":
        print(render_recent_results(Path(args.arguments[0])))
        return 0
    if not args.arguments:
        parser.error("prune-trials requires path")
    prune_trial_artifacts(Path(args.arguments[0]), args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
