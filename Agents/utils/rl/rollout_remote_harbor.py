#!/usr/bin/env python3
"""Miles/Polar-compatible HTTP front door for Harbor rollout mode.

The HTTP server only accepts RL requests, writes them to a local queue, and
waits for zellij workers to produce results.  Workers run the existing
harboropik.sh path so rollout mode keeps the same logs, local cache, Opik
hooks, and timeout finalization behavior as normal benchmark runs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET_NAME = os.environ.get("RL_DATASET_NAME", "seta")
DEFAULT_DATASET_ROOT = Path(os.environ.get("RL_DATASET_ROOT", "/workspace/seta-env/Harbor-Dataset"))
DEFAULT_MODEL_NAME = os.environ.get("RL_MODEL_NAME", "minimax2.7")
DEFAULT_API_BASE = os.environ.get("RL_API_BASE", "")
DEFAULT_API_KEY = os.environ.get("RL_API_KEY", "")
DEFAULT_API_KEY_MODE = os.environ.get("RL_API_KEY_MODE", "static").strip().lower()
DEFAULT_OPIK_PROJECT_NAME = os.environ.get("OPIK_PROJECT_NAME", "")
DEFAULT_DISABLED_TASK_IDS = os.environ.get("RL_DISABLED_TASK_IDS", "")
DEFAULT_TIMEOUT = float(os.environ.get("RL_REQUEST_TIMEOUT", "3600"))
TRACE_LOG = Path(os.environ.get("RL_TRACE_LOG", "/workspace/runs/rl-rollout-requests.jsonl"))
QUEUE_DIR = Path(os.environ.get("RL_QUEUE_DIR", "/workspace/runs/rl-rollout-queue"))
PENDING_DIR = QUEUE_DIR / "pending"
RESULTS_DIR = QUEUE_DIR / "results"
ACTIVE_DIR = Path(os.environ.get("RL_ACTIVE_DIR", str(QUEUE_DIR / "active")))
JOB_QUEUE_ROOT = Path(os.environ.get("RL_JOB_QUEUE_ROOT", str(QUEUE_DIR / "jobs")))
JOB_RUNTIME_ROOT = Path(os.environ.get("RL_JOB_RUNTIME_ROOT", str(TRACE_LOG.parent / "rl-jobs")))
ENABLE_DYNAMIC_JOB_ZELLIJ = os.environ.get("RL_DYNAMIC_JOB_ZELLIJ", "1").strip().lower() not in {"0", "false", "no", "off"}
JOB_ZELLIJ_LOCKS: dict[str, threading.Lock] = {}
JOB_ZELLIJ_READY: dict[str, str] = {}
JOB_ZELLIJ_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_trace(event: dict[str, Any]) -> None:
    TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    event = {k: v for k, v in event.items() if k != "api_key"}
    with TRACE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _metadata(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("metadata")
    return value if isinstance(value, dict) else {}


def _trial_config(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("trial_config")
    return value if isinstance(value, dict) else {}


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_slug(value: str, *, fallback: str = "default") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return slug or fallback


def _short_suffix(value: str, width: int = 6) -> str:
    value = str(value or "").strip()
    return value[-width:] if value else ""


def _extract_ray_submission_id(request: dict[str, Any]) -> str:
    ray_submission_id = _first_nonempty(request.get("ray_submission_id"))
    if not ray_submission_id:
        raise ValueError("top-level ray_submission_id is required in rollout mode")
    return ray_submission_id


def _extract_opik_project_name(request: dict[str, Any], ray_submission_id: str) -> str:
    return _first_nonempty(
        request.get("opik_project_name"),
        ray_submission_id,
        DEFAULT_OPIK_PROJECT_NAME,
    )


def _extract_polar_task_id(request: dict[str, Any], session_id: str) -> str:
    meta = _metadata(request)
    trial = _trial_config(request)
    return _first_nonempty(
        request.get("polar_task_id"),
        request.get("polar_task"),
        request.get("rl_task_id"),
        meta.get("polar_task_id"),
        meta.get("polar_task"),
        meta.get("rl_task_id"),
        trial.get("polar_task_id"),
        trial.get("polar_task"),
        trial.get("rl_task_id"),
        request.get("session_id"),
        session_id,
    )


def _display_name(task_name: str, polar_task_id: str, session_id: str) -> str:
    suffix = _short_suffix(polar_task_id or session_id)
    return f"{task_name}-{suffix}" if suffix else task_name


def _queue_for_submission(ray_submission_id: str) -> Path:
    if not ray_submission_id:
        return QUEUE_DIR
    return JOB_QUEUE_ROOT / _safe_slug(ray_submission_id)


def _submission_session_name(ray_submission_id: str, dataset_name: str) -> str:
    agent_slug = _safe_slug(os.environ.get("RL_AGENT", "claude-code"))
    dataset_slug = _safe_slug(dataset_name)
    submission_slug = _safe_slug(ray_submission_id)
    return f"harbor-rollout-{agent_slug}-{dataset_slug}-{submission_slug}"


def _job_lock(job_slug: str) -> threading.Lock:
    with JOB_ZELLIJ_LOCKS_GUARD:
        lock = JOB_ZELLIJ_LOCKS.get(job_slug)
        if lock is None:
            lock = threading.Lock()
            JOB_ZELLIJ_LOCKS[job_slug] = lock
        return lock


def _run_helper(cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill the whole process group; otherwise a timed-out helper can leave
        # child flock processes behind and block every following RL request.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise TimeoutError(
            f"{cmd!r} timed out after {timeout:.1f}s; "
            f"stdout={stdout.strip()!r}; stderr={stderr.strip()!r}"
        ) from exc
    return proc.returncode, stdout, stderr


def _zellij_session_exists(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["zellij", "list-sessions", "--short"],
            cwd=str(SCRIPT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return session_name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _cached_job_session(job_slug: str) -> str:
    with JOB_ZELLIJ_LOCKS_GUARD:
        return JOB_ZELLIJ_READY.get(job_slug, "")


def _clear_cached_job_session(job_slug: str, session_name: str) -> None:
    with JOB_ZELLIJ_LOCKS_GUARD:
        if JOB_ZELLIJ_READY.get(job_slug) == session_name:
            JOB_ZELLIJ_READY.pop(job_slug, None)


def _ensure_submission_zellij(
    ray_submission_id: str,
    dataset_name: str,
    queue_dir: Path,
    model_name: str,
    opik_project_name: str,
) -> str:
    if not ray_submission_id:
        raise ValueError("ray_submission_id is required in rollout mode so a worker zellij session can be started")
    if not ENABLE_DYNAMIC_JOB_ZELLIJ:
        raise RuntimeError("RL_DYNAMIC_JOB_ZELLIJ=0 is unsupported without a prestarted worker pool")
    submission_slug = _safe_slug(ray_submission_id)
    expected_session = _submission_session_name(ray_submission_id, dataset_name)
    ready_session = _cached_job_session(submission_slug)
    if ready_session:
        if _zellij_session_exists(ready_session):
            return ready_session
        _clear_cached_job_session(submission_slug, ready_session)

    lock = _job_lock(submission_slug)
    with lock:
        ready_session = _cached_job_session(submission_slug)
        if ready_session:
            if _zellij_session_exists(ready_session):
                return ready_session
            _clear_cached_job_session(submission_slug, ready_session)

        script = SCRIPT_DIR / "ensure_rl_job_zellij.sh"
        if not script.exists():
            raise FileNotFoundError(f"job zellij helper not found: {script}")
        env = os.environ.copy()
        env.update({
            "RL_ZELLIJ_SUBMISSION_ID": ray_submission_id,
            "RL_ZELLIJ_JOB_QUEUE_DIR": str(queue_dir),
            "RL_JOB_RUNTIME_ROOT": str(JOB_RUNTIME_ROOT),
            "RL_MODEL_NAME": model_name,
            "OPIK_PROJECT_NAME": opik_project_name,
        })
        returncode, stdout, stderr = _run_helper(
            [str(script), ray_submission_id, dataset_name, str(queue_dir)],
            cwd=str(SCRIPT_DIR),
            env=env,
            timeout=float(os.environ.get("RL_JOB_ZELLIJ_START_TIMEOUT", "45")),
        )
        if returncode != 0:
            raise RuntimeError(
                "failed to ensure submission zellij session "
                f"for ray_submission_id={ray_submission_id!r}: {stderr or stdout}"
            )
        session_name = stdout.strip().splitlines()[-1] if stdout.strip() else expected_session
        with JOB_ZELLIJ_LOCKS_GUARD:
            JOB_ZELLIJ_READY[submission_slug] = session_name
        return session_name


def _parse_task_ids(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").replace(";", ",").split(",") if item.strip()}


def _disabled_task_ids() -> set[str]:
    return _parse_task_ids(DEFAULT_DISABLED_TASK_IDS)


def _dataset_roots() -> dict[str, Path]:
    roots = {DEFAULT_DATASET_NAME: DEFAULT_DATASET_ROOT}
    raw_roots = os.environ.get("RL_DATASET_ROOTS", "")
    for item in raw_roots.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, path = item.split("=", 1)
            roots[name.strip()] = Path(path.strip())
        else:
            roots[Path(item).name] = Path(item)
    return roots


def _task_sort_key(path: Path) -> tuple[int, int | str]:
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def _dataset_root(dataset_name: str | None = None, dataset_root: str | None = None) -> Path:
    roots = _dataset_roots()
    root = Path(dataset_root) if dataset_root else roots.get(dataset_name or DEFAULT_DATASET_NAME)
    if root is None:
        raise ValueError(f"unknown dataset_name={dataset_name!r}; known={sorted(roots)}")
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"dataset root does not exist: {root}")
    return root


def list_dataset_tasks(
    dataset_name: str | None = None,
    dataset_root: str | None = None,
    *,
    include_disabled: bool = False,
) -> list[str]:
    root = _dataset_root(dataset_name, dataset_root)
    disabled = set() if include_disabled else _disabled_task_ids()
    return [
        path.name
        for path in sorted((item for item in root.iterdir() if item.is_dir()), key=_task_sort_key)
        if path.name not in disabled
    ]


def resolve_task_path(request: dict[str, Any]) -> Path:
    dataset_root = _dataset_root(request.get("dataset_name"), request.get("dataset_root"))
    raw_task = request.get("task_path") or request.get("task_id")
    if not raw_task:
        raise ValueError("task_id or task_path is required")
    task_path = Path(raw_task)
    if not task_path.is_absolute():
        task_path = dataset_root / task_path
    task_path = task_path.resolve()
    try:
        task_path.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError(f"task path {task_path} is outside dataset root {dataset_root}") from exc
    if not task_path.is_dir():
        raise FileNotFoundError(f"task path does not exist: {task_path}")
    if task_path.name in _disabled_task_ids():
        raise ValueError(f"task id {task_path.name} is disabled for dataset {request.get('dataset_name') or DEFAULT_DATASET_NAME}")
    return task_path


def _enqueue_request(request: dict[str, Any]) -> tuple[str, Path]:
    request_id = request.get("request_id") or uuid4().hex[:12]
    session_id = request.get("session_id") or uuid4().hex
    task_path = resolve_task_path(request)
    dataset_root = _dataset_root(request.get("dataset_name"), request.get("dataset_root"))
    dataset_name = request.get("dataset_name") or DEFAULT_DATASET_NAME
    model_name = request.get("model_name") or DEFAULT_MODEL_NAME
    ray_submission_id = _extract_ray_submission_id(request)
    opik_project_name = _extract_opik_project_name(request, ray_submission_id)
    polar_task_id = _extract_polar_task_id(request, session_id)
    display_name = _display_name(task_path.name, polar_task_id, session_id)
    queue_dir = _queue_for_submission(ray_submission_id)
    pending_dir = queue_dir / "pending"
    results_dir = queue_dir / "results"
    active_dir = queue_dir / "active"
    zellij_session = _ensure_submission_zellij(
        ray_submission_id,
        dataset_name,
        queue_dir,
        model_name,
        opik_project_name,
    )
    payload = {
        **request,
        "request_id": request_id,
        "session_id": session_id,
        "ray_submission_id": ray_submission_id,
        "polar_task_id": polar_task_id,
        "display_name": display_name,
        "task_id": task_path.name,
        "task_path": str(task_path),
        "dataset_name": dataset_name,
        "dataset_root": str(dataset_root),
        "model_name": model_name,
        "opik_project_name": opik_project_name,
        "api_base": request.get("api_base") or os.environ.get("RL_API_BASE", ""),
        "api_key": request.get("api_key") or DEFAULT_API_KEY,
        "api_key_mode": DEFAULT_API_KEY_MODE,
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
        "created_at": _now(),
    }
    pending_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_dir / f"{request_id}.json.tmp"
    final_path = pending_dir / f"{request_id}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(final_path)
    _append_trace({
        "event": "queued",
        "timestamp": _now(),
        "request_id": request_id,
        "task_id": task_path.name,
        "display_name": display_name,
        "session_id": session_id,
        "ray_submission_id": ray_submission_id,
        "polar_task_id": polar_task_id,
        "model_name": model_name,
        "opik_project_name": opik_project_name,
        "dataset_name": payload["dataset_name"],
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
    })
    return request_id, results_dir / f"{request_id}.json"


def _wait_result(result_path: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for rollout worker result: {result_path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-fleet-rl-rollout/0.2"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "status": "ok",
                    "mode": "rollout",
                    "dataset_roots": {name: str(path) for name, path in _dataset_roots().items()},
                    "disabled_task_ids": sorted(_disabled_task_ids()),
                    "default_dataset": DEFAULT_DATASET_NAME,
                    "default_agent": os.environ.get("RL_AGENT", "claude-code"),
                    "default_model_name": DEFAULT_MODEL_NAME,
                    "default_api_base_set": bool(DEFAULT_API_BASE),
                    "api_key_mode": DEFAULT_API_KEY_MODE,
                    "queue_dir": str(QUEUE_DIR),
                    "job_queue_root": str(JOB_QUEUE_ROOT),
                    "dynamic_job_zellij": ENABLE_DYNAMIC_JOB_ZELLIJ,
                    "trace_log": str(TRACE_LOG),
                })
                return
            if parsed.path == "/datasets":
                datasets = []
                for name, root in sorted(_dataset_roots().items()):
                    tasks = list_dataset_tasks(name)
                    datasets.append({"name": name, "root": str(root), "task_count": len(tasks), "disabled_task_ids": sorted(_disabled_task_ids())})
                self._send_json(HTTPStatus.OK, {"datasets": datasets})
                return
            prefix = "/datasets/"
            suffix = "/tasks"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                dataset_name = parsed.path[len(prefix):-len(suffix)].strip("/")
                dataset_root = (query.get("dataset_root") or [None])[0]
                include_disabled = (query.get("include_disabled") or ["false"])[0].lower() in {"1", "true", "yes"}
                tasks = list_dataset_tasks(dataset_name, dataset_root, include_disabled=include_disabled)
                self._send_json(HTTPStatus.OK, {"dataset_name": dataset_name, "task_count": len(tasks), "task_ids": tasks, "disabled_task_ids": sorted(_disabled_task_ids())})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": {"exception_type": type(exc).__name__, "exception_message": str(exc)}})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/run_trial":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        started = time.monotonic()
        request: dict[str, Any] = {}
        request_id = ""
        try:
            request = self._read_json()
            request_id, result_path = _enqueue_request(request)
            wait_timeout = float(request.get("request_timeout") or request.get("timeout") or DEFAULT_TIMEOUT)
            result = _wait_result(result_path, wait_timeout)
            _append_trace({
                "event": "returned",
                "timestamp": _now(),
                "request_id": request_id,
                "task_id": result.get("task_id"),
                "status": "completed" if result.get("ok") else "failed",
                "duration_sec": round(time.monotonic() - started, 3),
            })
            self._send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            detail = {"exception_type": type(exc).__name__, "exception_message": str(exc)}
            _append_trace({
                "event": "error",
                "timestamp": _now(),
                "request_id": request_id,
                "task_id": request.get("task_id") or request.get("task_path") or "<unknown>",
                "duration_sec": round(time.monotonic() - started, 3),
                "exception_info": detail,
            })
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": detail})
        except Exception as exc:
            detail = {"exception_type": type(exc).__name__, "exception_message": str(exc)}
            _append_trace({
                "event": "error",
                "timestamp": _now(),
                "request_id": request_id,
                "task_id": request.get("task_id") or request.get("task_path") or "<unknown>",
                "duration_sec": round(time.monotonic() - started, 3),
                "exception_info": detail,
            })
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": detail})


def main() -> int:
    host = os.environ.get("RL_HOST", "0.0.0.0")
    port = int(os.environ.get("RL_PORT", "19001"))
    for path in (TRACE_LOG.parent, PENDING_DIR, ACTIVE_DIR, RESULTS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    print(f"RL rollout Harbor service listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
