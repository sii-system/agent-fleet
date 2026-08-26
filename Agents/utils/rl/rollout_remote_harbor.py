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
from hashlib import sha256
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
DEFAULT_ENVIRONMENT_TYPE = os.environ.get("RL_ENVIRONMENT_TYPE", "docker").strip().lower()
DEFAULT_DISABLED_TASK_IDS = os.environ.get("RL_DISABLED_TASK_IDS", "")
DEFAULT_TIMEOUT = float(os.environ.get("RL_REQUEST_TIMEOUT", "3600"))
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
COMMAND_ARG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
# The retired request spelling remains fail-closed, not a supported alias.
RETIRED_E2B_PREBUILT_TEMPLATE_KEY = "t" + "b_e2b_prebuilt_template"
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


def _environment_type(request: dict[str, Any]) -> str:
    trial_environment = _trial_config(request).get("environment")
    if not isinstance(trial_environment, dict):
        trial_environment = {}
    value = _first_nonempty(
        request.get("environment_type"),
        trial_environment.get("type"),
        DEFAULT_ENVIRONMENT_TYPE,
        "docker",
    ).lower()
    if value not in {"docker", "e2b", "opensandbox", "qz"}:
        raise ValueError(
            "environment_type must be docker, e2b, opensandbox, or qz, "
            f"got: {value}"
        )
    return value


def _reject_e2b_credentials(request: dict[str, Any]) -> None:
    for key in ("e2b_api_key", "sbx_api_key", "qz_sandbox_api_key"):
        if _contains_key(request, key):
            raise ValueError(
                "sandbox API keys must be supplied by the agent-fleet host "
                "environment, not the request"
            )
    for key in (
        "e2b_template",
        RETIRED_E2B_PREBUILT_TEMPLATE_KEY,
        "harbor_e2b_prebuilt_template",
        "rl_e2b_prebuilt_template",
        "qz_sandbox_template",
    ):
        if _contains_key(request, key):
            raise ValueError(
                "the sandbox template must be supplied by the agent-fleet "
                "host environment, not the request"
            )


def _reject_model_credentials(request: dict[str, Any]) -> None:
    if _contains_key(request, "api_key"):
        raise ValueError(
            "model API keys must be supplied by the agent-fleet host environment, not the request"
        )


def _contains_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower() == target or _contains_key(item, target)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


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


def _is_relative_path(value: Path) -> bool:
    return not value.is_absolute() and ".." not in value.parts


def _require_contained_path(path: Path, root: Path, *, label: str) -> Path:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} {resolved_path} is outside trusted root {resolved_root}") from exc
    return resolved_path


def _storage_id(value: str, *, prefix: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _validated_request_id(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return uuid4().hex[:12]
    request_id = str(value).strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id may contain only letters, digits, dot, underscore, and hyphen, up to 128 characters")
    return request_id


def _validated_command_arg_id(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if not COMMAND_ARG_ID_RE.fullmatch(text):
        raise ValueError(f"{label} may contain only letters, digits, dot, underscore, and hyphen")
    return text



def _short_suffix(value: str, width: int = 6) -> str:
    value = str(value or "").strip()
    return value[-width:] if value else ""


def _extract_ray_submission_id(request: dict[str, Any]) -> str:
    ray_submission_id = _first_nonempty(request.get("ray_submission_id"))
    if not ray_submission_id:
        raise ValueError("top-level ray_submission_id is required in rollout mode")
    return _validated_command_arg_id(ray_submission_id, label="ray_submission_id")


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


def _extract_api_base(request: dict[str, Any]) -> str:
    api_base = _allowed_request_value(request, "api_base", "trial_config.agent.kwargs.api_base")
    if api_base == "":
        api_base = os.environ.get("RL_API_BASE", "")
    if api_base == "":
        return ""
    api_base = str(api_base).strip()
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("api_base must be an http or https URL with a host")
    if any(ord(char) < 32 for char in api_base):
        raise ValueError("api_base cannot contain control characters")
    return api_base


def _display_name(task_name: str, polar_task_id: str, session_id: str) -> str:
    suffix = _short_suffix(polar_task_id or session_id)
    return f"{task_name}-{suffix}" if suffix else task_name


def _queue_for_submission(ray_submission_id: str) -> Path:
    if not ray_submission_id:
        return _require_contained_path(QUEUE_DIR, QUEUE_DIR, label="queue dir")
    return _require_contained_path(JOB_QUEUE_ROOT / _storage_id(ray_submission_id, prefix="submission"), JOB_QUEUE_ROOT, label="queue dir")


def _submission_session_name(ray_submission_id: str, dataset_name: str) -> str:
    listener_identity = "\0".join(
        (
            os.environ.get("RL_AGENT", "claude-code"),
            dataset_name,
            str(JOB_QUEUE_ROOT),
            str(JOB_RUNTIME_ROOT),
            ray_submission_id,
        )
    )
    session_slug = _storage_id(listener_identity, prefix="session")
    # Zellij includes the session name in its Unix socket path. Keep the name
    # compact while preserving the listener namespace in the digest. Zellij
    # sessions are global to the local user, not scoped to one listener process.
    session_digest = session_slug.removeprefix("session-")
    return f"hr-{session_digest}"


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
            capture_output=True,
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
    submission_slug = _storage_id(ray_submission_id, prefix="submission")
    expected_session = _submission_session_name(ray_submission_id, dataset_name)
    session_key = expected_session
    ready_session = _cached_job_session(session_key)
    if ready_session:
        if _zellij_session_exists(ready_session):
            return ready_session
        _clear_cached_job_session(session_key, ready_session)

    lock = _job_lock(session_key)
    with lock:
        ready_session = _cached_job_session(session_key)
        if ready_session:
            if _zellij_session_exists(ready_session):
                return ready_session
            _clear_cached_job_session(session_key, ready_session)

        script = SCRIPT_DIR / "ensure_rl_job_zellij.sh"
        if not script.exists():
            raise FileNotFoundError(f"job zellij helper not found: {script}")
        env = os.environ.copy()
        env.update({
            "RL_ZELLIJ_SUBMISSION_ID": ray_submission_id,
            "RL_ZELLIJ_SUBMISSION_STORAGE_ID": submission_slug,
            "RL_ZELLIJ_SESSION_NAME": expected_session,
            "RL_ZELLIJ_JOB_QUEUE_DIR": str(queue_dir),
            "RL_JOB_QUEUE_ROOT": str(JOB_QUEUE_ROOT),
            "RL_JOB_RUNTIME_ROOT": str(JOB_RUNTIME_ROOT),
            "RL_MODEL_NAME": model_name,
            "OPIK_PROJECT_NAME": opik_project_name,
        })
        returncode, stdout, stderr = _run_helper(
            [str(script)],
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
        if session_name != expected_session:
            raise RuntimeError(
                "job zellij helper returned an unexpected session name: "
                f"expected={expected_session!r}, actual={session_name!r}"
            )
        with JOB_ZELLIJ_LOCKS_GUARD:
            JOB_ZELLIJ_READY[session_key] = session_name
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
    return {name: root.resolve() for name, root in roots.items()}


def _task_sort_key(path: Path) -> tuple[int, int | str]:
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def _dataset_root(dataset_name: str | None = None, dataset_root: str | None = None) -> Path:
    roots = _dataset_roots()
    selected_dataset = dataset_name or DEFAULT_DATASET_NAME
    _validated_command_arg_id(selected_dataset, label="dataset_name")
    root = roots.get(selected_dataset)
    if root is None:
        raise ValueError(f"unknown dataset_name={dataset_name!r}; known={sorted(roots)}")
    if dataset_root:
        requested_root = os.path.normpath(str(dataset_root).strip())
        matched_root = next((configured_root for configured_root in roots.values() if requested_root == str(configured_root)), None)
        if matched_root is None:
            raise ValueError("dataset_root must match a configured dataset root")
        if matched_root != root:
            raise ValueError("dataset_root does not match dataset_name")
    if not root.is_dir():
        raise FileNotFoundError(f"dataset root does not exist: {root}")
    return root


def _allowed_request_value(request: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = request
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value not in (None, ""):
            return value
    return ""


def _worker_options(request: dict[str, Any]) -> dict[str, Any]:
    option_paths = {
        "force_build": ("force_build", "trial_config.environment.force_build"),
        "max_new_tokens": ("max_new_tokens", "trial_config.agent.kwargs.max_new_tokens"),
        "model_info": ("model_info", "trial_config.agent.kwargs.model_info"),
        "claude_code_max_output_tokens": (
            "claude_code_max_output_tokens",
            "trial_config.agent.kwargs.claude_code_max_output_tokens",
        ),
        "max_turns": ("max_turns", "trial_config.agent.kwargs.max_turns"),
        "temperature": ("temperature", "trial_config.agent.kwargs.temperature", "trial_config.agent.kwargs.llm_kwargs.temperature"),
        "top_p": ("top_p", "trial_config.agent.kwargs.llm_kwargs.top_p", "trial_config.agent.kwargs.top_p"),
        "top_k": ("top_k", "trial_config.agent.kwargs.llm_kwargs.top_k", "trial_config.agent.kwargs.top_k"),
        "min_p": ("min_p", "trial_config.agent.kwargs.llm_kwargs.min_p", "trial_config.agent.kwargs.min_p"),
        "llm_timeout": ("llm_timeout", "trial_config.agent.kwargs.llm_kwargs.timeout"),
        "llm_max_retries": ("llm_max_retries", "trial_config.agent.kwargs.llm_kwargs.max_retries"),
        "agent_timeout_multiplier": ("agent_timeout_multiplier", "trial_config.agent.agent_timeout_multiplier"),
        "collect_rollout_details": ("collect_rollout_details", "trial_config.agent.kwargs.collect_rollout_details"),
        "enable_summarize": ("enable_summarize", "trial_config.agent.kwargs.enable_summarize"),
    }
    return {
        key: value
        for key, paths in option_paths.items()
        if (value := _allowed_request_value(request, *paths)) != ""
    }


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
    task_id = str(raw_task).strip()
    task_path = Path(task_id)
    if not task_id or not _is_relative_path(task_path) or len(task_path.parts) != 1:
        raise ValueError("task_id or task_path must be a relative path inside the configured dataset root")
    for candidate in dataset_root.iterdir():
        if candidate.name == task_id and candidate.is_dir():
            task_path = _require_contained_path(candidate, dataset_root, label="task path")
            if task_path.name in _disabled_task_ids():
                raise ValueError(f"task id {task_path.name} is disabled for dataset {request.get('dataset_name') or DEFAULT_DATASET_NAME}")
            return task_path
    raise FileNotFoundError(f"task path does not exist: {dataset_root / task_id}")


def _enqueue_request(request: dict[str, Any]) -> tuple[str, Path]:
    _reject_e2b_credentials(request)
    _reject_model_credentials(request)
    request_id = _validated_request_id(request.get("request_id"))
    request_file_id = _storage_id(request_id, prefix="request")
    session_id = request.get("session_id") or uuid4().hex
    task_path = resolve_task_path(request)
    dataset_root = _dataset_root(request.get("dataset_name"), request.get("dataset_root"))
    dataset_name = request.get("dataset_name") or DEFAULT_DATASET_NAME
    model_name = request.get("model_name") or DEFAULT_MODEL_NAME
    ray_submission_id = _extract_ray_submission_id(request)
    opik_project_name = _extract_opik_project_name(request, ray_submission_id)
    polar_task_id = _extract_polar_task_id(request, session_id)
    display_name = _display_name(task_path.name, polar_task_id, session_id)
    environment_type = _environment_type(request)
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
        "request_id": request_id,
        "request_file_id": request_file_id,
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
        "api_base": _extract_api_base(request),
        "api_key_mode": DEFAULT_API_KEY_MODE,
        "environment_type": environment_type,
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
        "created_at": _now(),
    }
    payload.update(_worker_options(request))
    pending_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_dir / f"{request_file_id}.json.tmp"
    final_path = pending_dir / f"{request_file_id}.json"
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
        "environment_type": environment_type,
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
    })
    return request_id, results_dir / f"{request_file_id}.json"


def _wait_result(result_path: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for rollout worker result: {result_path}")


class RolloutHTTPServer(ThreadingHTTPServer):
    request_queue_size = 1024


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
            raise TypeError("request body must be a JSON object")
        return data

    def _send_failure(
        self,
        status: int,
        exc: Exception,
        *,
        started: float,
        request: dict[str, Any],
        request_id: str,
    ) -> None:
        detail = {"exception_type": type(exc).__name__, "exception_message": str(exc)}
        _append_trace({
            "event": "error",
            "timestamp": _now(),
            "request_id": request_id,
            "task_id": request.get("task_id") or request.get("task_path") or "<unknown>",
            "duration_sec": round(time.monotonic() - started, 3),
            "exception_info": detail,
        })
        self._send_json(status, {"detail": detail})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self) -> None:
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
                    "default_environment_type": DEFAULT_ENVIRONMENT_TYPE,
                    "e2b_prebuilt_template_configured": bool(
                        os.environ.get("RL_E2B_PREBUILT_TEMPLATE")
                        or os.environ.get("HARBOR_E2B_PREBUILT_TEMPLATE")
                        or os.environ.get("E2B_TEMPLATE")
                    ),
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
        except Exception as exc:  # noqa: BLE001 - HTTP boundary returns structured failures
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": {"exception_type": type(exc).__name__, "exception_message": str(exc)}})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/run_trial":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        started = time.monotonic()
        request: dict[str, Any] = {}
        request_id = ""
        try:
            request = self._read_json()
            wait_timeout = float(request.get("request_timeout") or request.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError) as exc:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 - HTTP boundary returns structured failures
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )
            return
        try:
            request_id, result_path = _enqueue_request(request)
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
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001 - HTTP boundary returns structured failures
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )


def main() -> int:
    host = os.environ.get("RL_HOST", "0.0.0.0")
    port = int(os.environ.get("RL_PORT", "19001"))
    for path in (TRACE_LOG.parent, PENDING_DIR, ACTIVE_DIR, RESULTS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    print(f"RL rollout Harbor service listening on {host}:{port}", flush=True)
    RolloutHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
