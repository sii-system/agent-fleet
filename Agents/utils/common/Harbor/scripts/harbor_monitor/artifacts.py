"""Read and write Harbor monitor artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def to_float_value(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.lower() in {"none", "null", "nil"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_bool_value(raw: str | bool | None) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"true", "1", "yes", "y", "on"}:
            return True
        if value in {"false", "0", "no", "off", "none", "null"}:
            return False
        return None
    if isinstance(raw, (int, float)):
        return bool(raw)
    return None


def parse_lines(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        lines.append(raw.split("\t"))
    return lines


def parse_float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return to_float_value(value)
    return None


@dataclass
class TaskInput:
    task_index: str
    task_name: str = ""
    reward_raw: str | None = None
    exception_type: str | None = None
    result_path: str | None = None
    rc: str | None = None
    early_stop_reason: str | None = None
    in_done: bool = False
    in_failed: bool = False


@dataclass
class HarborJobSnapshot:
    total: int
    claimed: int
    remaining: int
    running: int
    tasks: dict[str, TaskInput]
    result_path: Path
    finished: bool


def _int_field(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key, 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _load_result_payload(result_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or "n_total_trials" not in payload
        or not isinstance(payload.get("stats"), dict)
    ):
        return None
    return payload


def _find_harbor_job_result_path(job_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    direct = job_dir / "result.json"
    payload = _load_result_payload(direct) if direct.is_file() else None
    if payload is not None:
        return direct, payload

    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for result_path in job_dir.rglob("result.json"):
        payload = _load_result_payload(result_path)
        if payload is None:
            continue
        try:
            mtime = result_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, result_path, payload))
    if not candidates:
        return None

    _, result_path, payload = max(candidates, key=lambda item: item[0])
    return result_path, payload


def load_harbor_job_snapshot(job_dir: Path) -> HarborJobSnapshot | None:
    """Read one native Harbor job without translating it into queue files."""
    found = _find_harbor_job_result_path(job_dir)
    if found is None:
        return None
    result_path, payload = found

    stats = payload["stats"]
    total = _int_field(payload, "n_total_trials")
    running = _int_field(stats, "n_running_trials")
    remaining = _int_field(stats, "n_pending_trials")
    claimed = max(0, total - remaining)
    tasks: dict[str, TaskInput] = {}
    for trial_result_path in sorted(result_path.parent.glob("*/result.json")):
        try:
            trial = json.loads(trial_result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(trial, dict):
            continue
        trial_name = str(trial.get("trial_name") or trial_result_path.parent.name)
        task_name = str(trial.get("task_name") or trial_name)
        exception = trial.get("exception_info")
        exception_type = ""
        if isinstance(exception, dict):
            exception_type = str(exception.get("exception_type") or "")
        tasks[trial_name] = TaskInput(
            task_index=trial_name,
            task_name=task_name,
            exception_type=exception_type,
            result_path=str(trial_result_path),
            in_done=True,
        )

    return HarborJobSnapshot(
        total=total,
        claimed=claimed,
        remaining=remaining,
        running=running,
        tasks=tasks,
        result_path=result_path,
        finished=bool(payload.get("finished_at")),
    )


def load_task_records(done_path: Path, failed_path: Path) -> dict[str, TaskInput]:
    tasks: dict[str, TaskInput] = {}

    for row in parse_lines(done_path):
        if len(row) < 2:
            continue
        task_index = row[0].strip()
        if not task_index:
            continue
        record = tasks.setdefault(
            task_index,
            TaskInput(task_index=task_index),
        )
        record.in_done = True
        record.task_name = row[1].strip() if len(row) > 1 else ""
        record.reward_raw = row[2].strip() if len(row) > 2 and row[2] is not None else None
        record.exception_type = row[3].strip() if len(row) > 3 and row[3] is not None else None
        record.result_path = row[4].strip() if len(row) > 4 and row[4] is not None else None

    for row in parse_lines(failed_path):
        if len(row) < 2:
            continue
        task_index = row[0].strip()
        if not task_index:
            continue
        record = tasks.setdefault(
            task_index,
            TaskInput(task_index=task_index),
        )
        record.in_failed = True
        record.task_name = row[1].strip() if len(row) > 1 else ""
        record.rc = row[2].strip() if len(row) > 2 and row[2] is not None else None
        record.early_stop_reason = row[3].strip() if len(row) > 3 and row[3] is not None else None
    return tasks


def resolve_result_path(result_path: str | None, base_dirs: list[Path]) -> Path | None:
    if not result_path:
        return None
    path = Path(result_path)
    if path.is_absolute():
        return path
    for base_dir in base_dirs:
        candidate = base_dir / path
        if candidate.exists():
            return candidate
    return base_dirs[0] / path if base_dirs else path


def read_result_json(result_path: str | None, base_dirs: list[Path]) -> tuple[bool, float | None, bool | None, str | None]:
    path = resolve_result_path(result_path, base_dirs)
    if path is None:
        return False, None, None, None
    if not path.exists():
        return False, None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None, None, None

    verifier = payload.get("verifier_result") if isinstance(payload, dict) else None
    if not isinstance(verifier, dict):
        return True, None, None, None

    rewards = verifier.get("rewards")
    reward_json = None
    if isinstance(rewards, dict):
        reward_json = parse_float_or_none(rewards.get("reward"))
    elif "reward" in verifier:
        reward_json = parse_float_or_none(verifier.get("reward"))

    is_resolved = None
    if "is_resolved" in verifier:
        is_resolved = to_bool_value(verifier.get("is_resolved"))

    record_exception = payload.get("exception_info")
    exception_type = None
    if isinstance(record_exception, dict):
        exception_type = str(record_exception.get("exception_type") or record_exception.get("type") or "")
        if exception_type == "None":
            exception_type = None

    # prioritize done.txt exception_type in caller
    return True, reward_json, is_resolved, exception_type

def parse_environment_events(raw: str | None) -> dict[str, Any] | list[Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, (dict, list)) else {}
    except json.JSONDecodeError:
        return {}
def read_int(path: Path, default: int | None = None) -> int | None:
    if not path.exists():
        return default
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default


def read_text_first_line(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8").strip()


def read_first_existing_text(paths: list[Path]) -> str:
    for path in paths:
        text = read_text_first_line(path, "")
        if text:
            return text
    return ""


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"retry_count": 0, "history": [], "adaptive_S": None}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"retry_count": 0, "history": [], "adaptive_S": None}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f"{state_path.name}.tmp")
    temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_path.replace(state_path)


def write_json(path: Path | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    manifest: dict[str, str] = {}
    for row in parse_lines(path):
        if not row:
            continue
        manifest[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    return manifest


def load_task_file_manifest(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    manifest: dict[str, str] = {}
    index = 1
    for raw in path.read_text(encoding="utf-8").splitlines():
        task_name = raw.strip()
        if not task_name or task_name.startswith("#"):
            continue
        manifest[str(index)] = task_name
        index += 1
    return manifest
