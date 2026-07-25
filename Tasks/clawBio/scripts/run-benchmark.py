#!/usr/bin/env python3
"""Run ClawBio skill tasks in parallel across Dockerized OpenClaw instances.

This script assumes the fleet has already been set up via Agents/Openclaw/scripts/setup.sh
and the clawbio plugin has been patched in via patch-plugin-config.sh.

Usage:
    ./run-benchmark.py --instances 3
    ./run-benchmark.py --instances 3 --config config/tasks.json
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
REPO_ROOT = BENCH_DIR.parent.parent
OPENCLAW_DIR = REPO_ROOT / "Agents" / "Openclaw"

RESULTS_DIR = BENCH_DIR / "results"
CONFIG_DIR = BENCH_DIR / "config"

TOKEN_RE = re.compile(r"^TOKEN_(\d+)=(.+)$")


# ── Instance discovery ──


def load_env_file(path: Path) -> dict[str, str]:
    """Load key=value pairs from an env file."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


@dataclass(frozen=True)
class Instance:
    """Represents a single OpenClaw instance."""

    index: int
    token: str
    container_name: str
    workspace_dir: Path


def discover_instances(max_instances: int | None = None) -> list[Instance]:
    """Discover instances from Agents/Openclaw/.env and docker inspect."""
    env_file = OPENCLAW_DIR / ".env"
    if not env_file.exists():
        raise SystemExit(
            f"Error: Fleet .env not found: {env_file}. "
            "Run Agents/Openclaw/scripts/setup.sh first."
        )

    env = load_env_file(env_file)
    prefix = env.get("CONTAINER_NAME_PREFIX", "openclaw")

    tokens: dict[int, str] = {}
    for key, value in env.items():
        m = re.match(r"^TOKEN_(\d+)$", key)
        if m:
            tokens[int(m.group(1))] = value

    if not tokens:
        raise SystemExit(f"Error: No TOKEN_N entries found in {env_file}.")

    instances: list[Instance] = []
    for index, token in sorted(tokens.items()):
        # Discover workspace dir from docker inspect (mount analysis)
        workspace_dir = _discover_workspace_dir(f"{prefix}-{index}")
        instances.append(
            Instance(
                index=index,
                token=token,
                container_name=f"{prefix}-{index}",
                workspace_dir=workspace_dir,
            )
        )

    if max_instances is not None:
        instances = instances[:max_instances]

    return instances


def _discover_workspace_dir(container: str) -> Path:
    """Find the host-side workspace mount path from docker inspect."""
    proc = subprocess.run(
        ["docker", "inspect", container, "--format", "{{json .Mounts}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Error: docker inspect failed for {container}")

    try:
        mounts = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"Error: could not parse mount info for {container}")

    for mount in mounts:
        dest = mount.get("Destination", "")
        if dest == "/home/node/workspace":
            source = mount.get("Source", "")
            if source:
                return Path(source)

    raise SystemExit(
        f"Error: no /home/node/workspace mount found in {container}. "
        "Ensure the fleet was started with WORKSPACE_BASE configured."
    )


# ── Task config ──


def load_task_config(path: Path) -> dict[str, Any]:
    """Load and validate task configuration file."""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "Error: task config is not JSON-compatible YAML and PyYAML is not installed. "
                "Either install PyYAML or keep the config file JSON-compatible."
            ) from exc
        data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise SystemExit(f"Error: task config must be a mapping: {path}")

    defaults = data.get("defaults") or {}
    tasks = data.get("tasks") or []

    if not isinstance(defaults, dict):
        raise SystemExit(f"Error: task config defaults must be a mapping: {path}")
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit(f"Error: task config must contain a non-empty tasks list: {path}")

    for task in tasks:
        if not isinstance(task, dict):
            raise SystemExit(f"Error: each task must be a mapping: {path}")
        for field in ("id", "prompt"):
            if not str(task.get(field, "")).strip():
                raise SystemExit(f"Error: task is missing required field '{field}': {task}")

    return {"defaults": defaults, "tasks": tasks}


# ── Docker helpers ──


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=text)


def docker_exec(container: str, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["docker", "exec", container, "bash", "-lc", command], check=check)


def get_container_logs(container: str, tail: int = 50) -> str:
    proc = subprocess.run(
        ["docker", "logs", container, "--tail", str(tail)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.stdout or ""


def check_container_errors(container: str, since_seconds: int = 30) -> list[str]:
    proc = subprocess.run(
        ["docker", "logs", container, "--since", f"{since_seconds}s"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    logs = proc.stdout or ""
    errors = []
    for line in logs.splitlines():
        line_lower = line.lower()
        if any(err in line_lower for err in ["error", "failed", "exception", "traceback"]):
            errors.append(line.strip())
    return errors


def check_sandbox_errors(container: str, since_seconds: int = 30) -> list[str]:
    proc = subprocess.run(
        ["docker", "logs", container, "--since", f"{since_seconds}s"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    logs = proc.stdout or ""
    sandbox_errors = []
    for line in logs.splitlines():
        if "path escapes sandbox" in line.lower() or "workspaceonly" in line.lower():
            sandbox_errors.append(line.strip())
    return sandbox_errors


def inspect_container_state(container: str) -> dict[str, Any]:
    proc = run_command(["docker", "inspect", container], check=False)
    if proc.returncode != 0:
        raise SystemExit(f"Error: Docker container not found: {container}")
    payload = json.loads(proc.stdout)
    if not payload:
        raise SystemExit(f"Error: docker inspect returned no state for: {container}")
    return payload[0].get("State") or {}


def wait_for_container_healthy(container: str, timeout_seconds: int = 180) -> str:
    deadline = time.time() + timeout_seconds
    last_status = "unknown"
    while time.time() < deadline:
        state = inspect_container_state(container)
        status = state.get("Status") or "unknown"
        health = (state.get("Health") or {}).get("Status")
        last_status = health or status
        if status == "running" and (health == "healthy" or health is None):
            return last_status
        time.sleep(2)
    return last_status


def restart_container(instance: Instance, run_logger: RunLogger, worker_position: int, reason: str) -> None:
    run_logger.log(
        f"[worker {worker_position}] restarting {instance.container_name} {reason}"
    )
    proc = run_command(["docker", "restart", instance.container_name], check=False)
    if proc.returncode != 0:
        run_logger.log(
            f"[worker {worker_position}] restart failed for {instance.container_name}: "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
        return
    health = wait_for_container_healthy(instance.container_name)
    run_logger.log(
        f"[worker {worker_position}] {instance.container_name} restart health={health}"
    )


# ── Prerequisites ──


def ensure_prerequisites(instances: list[Instance], requested: int) -> list[Instance]:
    """Validate prerequisites before running benchmark."""
    if requested < 1:
        raise SystemExit("Error: --instances must be at least 1.")
    if len(instances) < requested:
        raise SystemExit(
            f"Error: requested {requested} worker(s), "
            f"but only {len(instances)} instance(s) are configured."
        )

    try:
        run_command(["docker", "info"])
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit("Error: docker is not available or daemon is not reachable.") from exc

    selected = instances[:requested]

    for instance in selected:
        state = inspect_container_state(instance.container_name)
        status = state.get("Status")
        if status != "running":
            raise SystemExit(
                f"Error: container is not running: {instance.container_name} (status={status})"
            )
        health = (state.get("Health") or {}).get("Status")
        if health == "unhealthy":
            raise SystemExit(f"Error: container health is unhealthy: {instance.container_name}")

    return selected


def recover_failed_instances(
    instances: list[Instance],
    failed_container_names: set[str],
    run_logger: RunLogger,
    *,
    health_timeout_seconds: int = 120,
) -> None:
    """Restart failed containers and wait until they are ready for next iteration."""
    if not failed_container_names:
        return

    selected = [i for i in instances if i.container_name in failed_container_names]
    if not selected:
        return

    run_logger.log(
        "Recovering failed instances before next iteration: "
        + ", ".join(i.container_name for i in selected)
    )
    for instance in selected:
        run_command(["docker", "restart", instance.container_name], check=True)

    deadline = time.time() + health_timeout_seconds
    pending = {i.container_name for i in selected}
    while pending and time.time() < deadline:
        resolved: set[str] = set()
        for container in pending:
            state = inspect_container_state(container)
            status = state.get("Status")
            health_obj = state.get("Health")
            health = (health_obj or {}).get("Status")
            # If a healthcheck exists, wait for explicit healthy instead of
            # treating "starting" as ready.
            if health_obj is not None:
                if status == "running" and health == "healthy":
                    resolved.add(container)
            elif status == "running":
                resolved.add(container)
        pending -= resolved
        if pending:
            time.sleep(2)

    if pending:
        raise SystemExit(
            "Error: failed to recover containers before next iteration: "
            + ", ".join(sorted(pending))
        )


# ── Task execution ──


def shard_tasks(tasks: list[dict[str, Any]], instance_count: int) -> list[list[dict[str, Any]]]:
    """Distribute tasks across instances using round-robin."""
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(instance_count)]
    for idx, task in enumerate(tasks):
        buckets[idx % instance_count].append(task)
    return buckets


class RunLogger:
    """Thread-safe logger that writes to both console and file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._lock:
            print(line, flush=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def sanitize_id(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return sanitized.strip("-") or "task"


def build_prompt(task: dict[str, Any], defaults: dict[str, Any]) -> str:
    """Return the prompt for a task, appending artifact output directive if configured."""
    prompt = str(task["prompt"]).strip()

    artifact_paths = task.get("artifact_paths") or defaults.get("artifact_paths") or []

    if artifact_paths:
        paths_str = ", ".join(f"{p}/" for p in artifact_paths)
        output_directive = f" Output all results to {paths_str}."
        if paths_str.rstrip("/") not in prompt:
            prompt = prompt + output_directive

    return prompt


def snapshot_artifacts(workspace_dir: Path, artifact_paths: list[str]) -> dict[str, dict[str, Any]]:
    """Take a snapshot of artifact files for change detection."""
    snapshot: dict[str, dict[str, Any]] = {}
    for rel_path in artifact_paths:
        root = resolve_workspace_path(workspace_dir, rel_path)
        if not root.exists():
            continue
        paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            snapshot[str(path)] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    return snapshot


def resolve_workspace_path(workspace_dir: Path, rel_path: str) -> Path:
    root = (workspace_dir / rel_path).resolve()
    workspace_root = workspace_dir.resolve()
    if root == workspace_root:
        raise ValueError(f"artifact path resolves to workspace root: {rel_path}")
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes workspace: {rel_path}") from exc
    return root


def make_path_removable(root: Path) -> None:
    if sys.platform != "linux" or not root.exists():
        return
    chmod = shutil.which("chmod")
    if chmod is None:
        return

    proc = subprocess.run(
        [chmod, "-R", "a+rwX", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return

    sudo = shutil.which("sudo")
    if sudo is not None:
        subprocess.run(
            [sudo, "-n", chmod, "-R", "a+rwX", str(root)],
            check=False,
            capture_output=True,
            text=True,
        )


def clear_artifact_paths(workspace_dir: Path, artifact_paths: list[str]) -> None:
    for rel_path in artifact_paths:
        root = resolve_workspace_path(workspace_dir, rel_path)
        if root.exists():
            make_path_removable(root)
        if root.is_dir():
            shutil.rmtree(root)
        elif root.exists():
            root.unlink()


def collect_artifacts(
    workspace_dir: Path,
    artifact_paths: list[str],
    before: dict[str, dict[str, Any]],
    artifacts_dir: Path,
) -> list[dict[str, Any]]:
    """Collect artifacts that changed since the before snapshot."""
    after = snapshot_artifacts(workspace_dir, artifact_paths)
    collected: list[dict[str, Any]] = []

    for source, meta in sorted(after.items()):
        previous = before.get(source)
        if previous == meta:
            continue

        source_path = Path(source)
        try:
            relative = source_path.relative_to(workspace_dir)
        except ValueError:
            relative = Path(source_path.name)

        dest = artifacts_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(source_path, dest)
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"Warning: failed to copy {source_path}: {e}")
            continue

        collected.append({
            "source": str(source_path),
            "copied_to": str(dest),
            "relative_path": str(relative),
            "size": meta["size"],
            "mtime_ns": meta["mtime_ns"],
        })

    return collected


def append_log(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if text and not text.endswith("\n"):
            fh.write("\n")


def task_session_id(task_id: str, started_at: datetime) -> str:
    timestamp = started_at.strftime("%Y%m%d%H%M%S%f")
    return f"clawbio-{task_id}-{timestamp}"


def snapshot_sessions(instance: Instance, session_path: Path) -> None:
    proc = docker_exec(instance.container_name, "openclaw sessions --json", check=False)
    session_path.write_text(proc.stdout or "{}", encoding="utf-8")


def parse_json_output(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def evaluate_agent_response(payload: dict[str, Any] | None) -> tuple[str, str]:
    """Return (status, error_message) from agent response."""
    if payload is None:
        return "failed", "Agent returned non-JSON response"

    result = payload.get("result", {})
    meta = result.get("meta", {})

    if meta.get("aborted"):
        payloads = result.get("payloads", [])
        error_msg = "Agent run was aborted"
        if payloads and isinstance(payloads, list) and payloads[0]:
            text = payloads[0].get("text", "")
            if "timed out" in text.lower():
                error_msg = f"Agent timed out: {text[:200]}"
            elif text:
                error_msg = f"Agent aborted: {text[:200]}"
        return "failed", error_msg

    payloads = result.get("payloads", [])
    if not payloads or all(not p.get("text") for p in payloads if isinstance(p, dict)):
        return "failed", "Agent returned empty response (no output generated)"

    return "success", ""


def _extract_expected_scripts(skill_name: str) -> list[str]:
    """Parse the skill's SKILL.md for .py script references.

    Returns a list of script basenames that the skill's documentation
    tells the agent to execute.  This covers both direct scripts
    (e.g. ``rnaseq_de.py``) and delegated scripts
    (e.g. ``drug-photo`` → ``pharmgx_reporter.py``).
    """
    skill_md = BENCH_DIR / "cache" / "clawbio" / "skills" / skill_name / "SKILL.md"
    if not skill_md.is_file():
        # Fallback: derive from skill name (e.g. rnaseq-de → rnaseq_de.py).
        return [skill_name.replace("-", "_") + ".py"]

    content = skill_md.read_text()
    # Collect every .py reference in the SKILL.md, then deduplicate by basename.
    py_refs = re.findall(r"(\S+\.py)", content)
    seen: set[str] = set()
    scripts: list[str] = []
    for ref in py_refs:
        basename = Path(ref).name
        if basename not in seen:
            seen.add(basename)
            scripts.append(basename)
    return scripts or [skill_name.replace("-", "_") + ".py"]


def _copy_session_jsonl(
    container: str, session_id: str, dest: Path, run_logger: RunLogger | None = None
) -> Path | None:
    """Copy the session JSONL file from a container to dest.

    Returns the path to the copied file, or None if not found.
    """
    jsonl_name = f"{session_id}.jsonl"
    container_path = f"/home/node/openclaw-state/agents/main/sessions/{jsonl_name}"
    proc = run_command(
        ["docker", "cp", f"{container}:{container_path}", str(dest / jsonl_name)],
        check=False,
    )
    if proc.returncode != 0:
        if run_logger:
            run_logger.log(f"Warning: could not copy session JSONL from {container}: {proc.stderr.strip()}")
        return None
    return dest / jsonl_name


_TOOLCALL_TYPES = {"toolcall", "tool_call", "toolCall", "tooluse", "tool_use"}


def _extract_exec_from_jsonl(jsonl_path: Path) -> list[str]:
    """Parse a session JSONL file and return all exec tool call commands.

    Handles the various tool call block shapes used by OpenClaw:
    - type: toolcall | tool_call | toolCall | tooluse | tool_use
    - args key: args | arguments | input
    """
    commands: list[str] = []
    if not jsonl_path.is_file():
        return commands
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type", "").lower().replace("_", "") not in {
                "toolcall", "tooluse"
            }:
                continue
            if block.get("name") != "exec":
                continue
            # Args can be under 'args', 'arguments', or 'input'.
            args = block.get("args") or block.get("arguments") or block.get("input") or {}
            cmd = args.get("command", "") if isinstance(args, dict) else ""
            if cmd:
                commands.append(cmd)
    return commands


def verify_skill_execution(
    task: dict[str, Any],
    session_jsonl_path: Path | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check that the agent exec'd a script documented by the skill.

    Uses session JSONL for exec command extraction when available (no
    openclaw patch required). Falls back to toolSummary.toolCalls from
    the agent response payload.

    Returns (ok, reason).  When *ok* is False the task should be marked failed.
    """
    skill_name = task.get("skill")
    if not skill_name:
        return True, ""

    expected_scripts = _extract_expected_scripts(skill_name)

    # Prefer session JSONL — has full command arguments without patching openclaw.
    if session_jsonl_path and session_jsonl_path.is_file():
        exec_commands = _extract_exec_from_jsonl(session_jsonl_path)
    else:
        # Fallback: use toolSummary.toolCalls (requires openclaw patch for args).
        result = (payload or {}).get("result", {})
        meta = result.get("meta", {})
        tool_calls = (meta.get("toolSummary") or {}).get("toolCalls") or []
        exec_commands = [c.get("args") or "" for c in tool_calls if c.get("toolName") == "exec"]

    for args_str in exec_commands:
        for script in expected_scripts:
            if script in args_str:
                return True, ""

    return False, (
        f"Agent never exec'd any of {expected_scripts}. "
        f"Exec'd commands: {exec_commands}"
    )


def run_task(
    instance: Instance,
    worker_position: int,
    task: dict[str, Any],
    defaults: dict[str, Any],
    worker_dir: Path,
    run_logger: RunLogger,
) -> dict[str, Any]:
    """Execute a single task and collect results."""
    task_id = sanitize_id(str(task["id"]))
    task_dir = worker_dir / task_id
    artifacts_dir = task_dir / "artifacts"
    task_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    task_log = task_dir / "task.log"
    response_path = task_dir / "agent-response.json"
    session_path = task_dir / "session-snapshot.json"
    session_jsonl_path = task_dir / "session.jsonl"
    metadata_path = task_dir / "metadata.json"
    container_log_path = task_dir / "container.log"

    prompt = build_prompt(task, defaults)
    timeout_seconds = int(task.get("timeout_seconds", defaults.get("timeout_seconds", 1800)))
    thinking = str(task.get("thinking", defaults.get("thinking", "medium")))
    artifact_paths = [
        str(p) for p in (task.get("artifact_paths") or defaults.get("artifact_paths") or [])
    ]

    clear_artifact_paths(instance.workspace_dir, artifact_paths)
    pre_snapshot = snapshot_artifacts(instance.workspace_dir, artifact_paths)

    started_at = datetime.now(timezone.utc)
    session_id = task_session_id(task_id, started_at)
    skill_info = f" (skill={task['skill']})" if task.get("skill") else ""
    run_logger.log(
        f"[worker {worker_position}] {instance.container_name} starting task {task_id}{skill_info}"
    )

    status = "success"
    failure_stage = ""
    error_message = ""
    agent_payload: Any = {}
    agent_finished = False

    try:
        prompt_json = json.dumps(prompt, ensure_ascii=False)
        cmd = (
            f"openclaw agent --json "
            f"--timeout {timeout_seconds} "
            f"--thinking {thinking} "
            f"--session-id {shlex.quote(session_id)} "
            f"--message {prompt_json}"
        )
        append_log(task_log, f"$ {cmd}\n")

        agent_proc = docker_exec(instance.container_name, cmd, check=False)
        append_log(task_log, agent_proc.stdout)
        append_log(task_log, agent_proc.stderr)
        response_path.write_text(agent_proc.stdout or "{}", encoding="utf-8")

        if agent_proc.returncode != 0:
            failure_stage = "task"
            raise RuntimeError(
                f"openclaw agent failed in {instance.container_name} "
                f"with exit code {agent_proc.returncode}"
            )

        payload = parse_json_output(agent_proc.stdout)
        agent_payload = payload if payload is not None else {"raw_output": agent_proc.stdout}

        eval_status, eval_error = evaluate_agent_response(payload)
        if eval_status == "failed":
            failure_stage = "agent"
            raise RuntimeError(eval_error)

        agent_finished = True

    except Exception as exc:  # noqa: BLE001 - task boundary records arbitrary failures
        status = "failed"
        if not failure_stage:
            failure_stage = "task"
        error_message = str(exc)
        container_logs = get_container_logs(instance.container_name, tail=100)
        container_log_path.write_text(container_logs, encoding="utf-8")
        sandbox_errors = check_sandbox_errors(instance.container_name, since_seconds=60)
        if sandbox_errors:
            failure_stage = "sandbox"
            error_message = (
                "Sandbox restriction blocking skill execution: skills cannot be read "
                "from extensions directory. Set tools.fs.workspaceOnly=false in openclaw.json."
            )
            run_logger.log(
                f"[worker {worker_position}] {instance.container_name} SANDBOX ERROR for task {task_id}:"
            )
            for err_line in sandbox_errors[:3]:
                run_logger.log(f"  {err_line}")
        container_errors = check_container_errors(instance.container_name, since_seconds=60)
        if container_errors and not sandbox_errors:
            run_logger.log(
                f"[worker {worker_position}] {instance.container_name} container errors for task {task_id}:"
            )
            for err_line in container_errors[:5]:
                run_logger.log(f"  {err_line}")
            run_logger.log(f"  See {container_log_path} for full logs")
    finally:
        snapshot_sessions(instance, session_path)
        copied_jsonl = _copy_session_jsonl(instance.container_name, session_id, task_dir, run_logger)
        if copied_jsonl:
            session_jsonl_path = copied_jsonl
        if not container_log_path.exists():
            container_logs = get_container_logs(instance.container_name, tail=50)
            container_log_path.write_text(container_logs, encoding="utf-8")

    collected = collect_artifacts(instance.workspace_dir, artifact_paths, pre_snapshot, artifacts_dir)

    # Post-check: verify the agent actually executed the skill, then that it
    # produced output files.
    if status == "success" and artifact_paths:
        # Layer 1: verify the agent exec'd the skill's Python script.
        exec_ok, exec_reason = verify_skill_execution(task, session_jsonl_path, agent_payload)
        if not exec_ok:
            status = "failed"
            failure_stage = "agent"
            error_message = exec_reason
        # Layer 2: verify files were produced in the configured output paths.
        elif len(collected) == 0:
            status = "failed"
            failure_stage = "artifacts"
            error_message = "Task produced no artifacts in configured output paths"

    finished_at = datetime.now(timezone.utc)
    duration_seconds = round((finished_at - started_at).total_seconds(), 3)

    agent_meta = agent_payload.get("result", {}).get("meta", {}) if isinstance(agent_payload, dict) else {}

    metadata = {
        "task_id": task_id,
        "skill": task.get("skill"),
        "prompt_source": "task-config",
        "status": status,
        "failure_stage": failure_stage or None,
        "error_message": error_message or None,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "instance": instance.index,
        "container_name": instance.container_name,
        "worker": worker_position,
        "session_id": session_id,
        "thinking": thinking,
        "timeout_seconds": timeout_seconds,
        "task_dir": str(task_dir),
        "task_log": str(task_log),
        "response_file": str(response_path),
        "session_snapshot_file": str(session_path),
        "container_log_file": str(container_log_path),
        "artifacts_dir": str(artifacts_dir),
        "artifact_count": len(collected),
        "artifacts": collected,
        "agent_aborted": agent_meta.get("aborted"),
        "agent_finished": agent_finished,
        "agent_has_output": bool(agent_payload.get("result", {}).get("payloads")) if isinstance(agent_payload, dict) else False,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    run_logger.log(
        f"[worker {worker_position}] {instance.container_name} finished task {task_id} "
        f"finished={agent_finished} status={status} duration={duration_seconds:.3f}s artifacts={len(collected)}"
    )

    return {
        "task_id": task_id,
        "skill": task.get("skill"),
        "status": status,
        "agent_finished": agent_finished,
        "failure_stage": failure_stage or None,
        "error_message": error_message or None,
        "duration_seconds": duration_seconds,
        "instance": instance.index,
        "container_name": instance.container_name,
        "worker": worker_position,
        "task_log": str(task_log),
        "response_file": str(response_path),
        "session_snapshot_file": str(session_path),
        "container_log_file": str(container_log_path),
        "metadata_file": str(metadata_path),
        "artifacts_dir": str(artifacts_dir),
        "artifacts": collected,
        "artifact_count": len(collected),
        "agent_response": agent_payload,
    }


# ── Worker & results ──


def run_worker(
    instance: Instance,
    worker_position: int,
    tasks: list[dict[str, Any]],
    defaults: dict[str, Any],
    instances_output_dir: Path,
    run_logger: RunLogger,
) -> dict[str, Any]:
    """Execute all tasks for a single worker/instance."""
    started = time.time()
    worker_dir = instances_output_dir / str(instance.index)
    worker_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for idx, task in enumerate(tasks):
        result = run_task(
            instance,
            worker_position,
            task,
            defaults,
            worker_dir,
            run_logger,
        )
        results.append(result)
        if result["status"] != "success" and idx < len(tasks) - 1:
            restart_container(
                instance,
                run_logger,
                worker_position,
                f"after failed task {result['task_id']}",
            )

    return {
        "instance": instance.index,
        "container_name": instance.container_name,
        "worker": worker_position,
        "task_count": len(tasks),
        "duration_seconds": round(time.time() - started, 3),
        "tasks": results,
    }


def build_results_payload(
    args: argparse.Namespace,
    task_config_path: Path,
    workers: list[dict[str, Any]],
    run_dir: Path,
    iteration: int | None = None,
) -> dict[str, Any]:
    """Build the aggregate results payload."""
    tasks = [task for worker in workers for task in worker["tasks"]]
    tasks.sort(key=lambda item: item["task_id"])

    success_count = sum(1 for task in tasks if task["status"] == "success")
    finished_count = sum(1 for task in tasks if task.get("agent_finished"))
    failure_count = len(tasks) - success_count
    durations = [float(task["duration_seconds"]) for task in tasks]

    totals = {
        "task_count": len(tasks),
        "finished_count": finished_count,
        "finished_rate": round(finished_count / len(tasks), 4) if tasks else 0.0,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": round(success_count / len(tasks), 4) if tasks else 0.0,
        "total_duration_seconds": round(sum(durations), 3),
        "avg_duration_seconds": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "max_duration_seconds": round(max(durations), 3) if durations else 0.0,
        "min_duration_seconds": round(min(durations), 3) if durations else 0.0,
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_file": str(task_config_path),
        "output_dir": str(run_dir),
        "workers": workers,
        "tasks": tasks,
        "totals": totals,
        "command": {
            "instances": args.instances,
        },
    }
    if iteration is not None:
        payload["iteration"] = iteration
    return payload


def validate_iteration_completion(payload: dict[str, Any], expected_task_count: int) -> tuple[bool, str]:
    """Ensure this iteration has terminal result entries for all expected tasks."""
    tasks = payload.get("tasks", [])
    if len(tasks) != expected_task_count:
        return (
            False,
            f"expected {expected_task_count} task results but got {len(tasks)}",
        )

    non_terminal = []
    for task in tasks:
        status = str(task.get("status", "")).strip().lower()
        if status not in {"success", "failed", "error"}:
            non_terminal.append(str(task.get("task_id", "unknown")))
    if non_terminal:
        return (False, f"non-terminal task statuses: {','.join(non_terminal)}")
    return (True, "")


def render_results_markdown(payload: dict[str, Any]) -> str:
    """Render results as a Markdown summary."""
    totals = payload["totals"]
    lines = [
        "# ClawBio Benchmark Results",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Config file: `{payload['config_file']}`",
        f"- Output dir: `{payload['output_dir']}`",
        f"- Tasks: `{totals['task_count']}`",
        f"- Finished: `{totals['finished_count']}` (`{totals['finished_rate']:.2%}`)",
        f"- Success: `{totals['success_count']}` (`{totals['success_rate']:.2%}`)",
        f"- Failure: `{totals['failure_count']}`",
        f"- Avg duration: `{totals['avg_duration_seconds']:.3f}s`",
        f"- Max duration: `{totals['max_duration_seconds']:.3f}s`",
        f"- Min duration: `{totals['min_duration_seconds']:.3f}s`",
        "",
        "## Per-Instance Summary",
        "",
        "| Worker | Instance | Container | Tasks | Duration (s) |",
        "|---|---:|---|---:|---:|",
    ]

    for worker in payload["workers"]:
        lines.append(
            f"| {worker['worker']} | {worker['instance']} | `{worker['container_name']}` | "
            f"{worker['task_count']} | {worker['duration_seconds']:.3f} |"
        )

    lines.extend([
        "",
        "## Task Results",
        "",
        "| Task | Skill | Finished | Status | Instance | Container | Duration (s) | Artifacts |",
        "|---|---|---|---|---:|---|---:|---|",
    ])

    for task in payload["tasks"]:
        skill_display = task.get("skill") or "-"
        finished = "Y" if task.get("agent_finished") else "N"
        lines.append(
            f"| `{task['task_id']}` | `{skill_display}` | {finished} | `{task['status']}` | "
            f"{task['instance']} | `{task['container_name']}` | "
            f"{task['duration_seconds']:.3f} | `{task['artifacts_dir']}` |"
        )

    failures = [task for task in payload["tasks"] if task["status"] != "success"]
    lines.extend(["", "## Failures", ""])

    if not failures:
        lines.append("No failed tasks.")
    else:
        for task in failures:
            lines.append(
                f"- `{task['task_id']}` on `{task['container_name']}` failed during "
                f"`{task.get('failure_stage') or 'unknown'}`: "
                f"{task.get('error_message') or 'no error message'}"
            )
            lines.append(f"  - Log: `{task['task_log']}`")

    return "\n".join(lines) + "\n"


def ensure_latest_link(output_dir: Path, run_dir: Path) -> None:
    latest = output_dir / "latest"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(run_dir.name, target_is_directory=True)
    except OSError as e:
        print(f"Warning: failed to update 'latest' symlink: {e}")


def build_iteration_summary(
    iteration: int,
    payload: dict[str, Any],
    iteration_dir: Path,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    totals = payload["totals"]
    failures = [task for task in payload["tasks"] if task["status"] != "success"]

    failure_stage_counts: dict[str, int] = {}
    for task in failures:
        stage = task.get("failure_stage") or "unknown"
        failure_stage_counts[stage] = failure_stage_counts.get(stage, 0) + 1

    return {
        "iteration": iteration,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_time_seconds": round((finished_at - started_at).total_seconds(), 3),
        "task_count": totals["task_count"],
        "finished_count": totals["finished_count"],
        "finished_rate": totals["finished_rate"],
        "success_count": totals["success_count"],
        "failure_count": totals["failure_count"],
        "success_rate": totals["success_rate"],
        "avg_duration_seconds": totals["avg_duration_seconds"],
        "max_duration_seconds": totals["max_duration_seconds"],
        "min_duration_seconds": totals["min_duration_seconds"],
        "failure_stage_counts": failure_stage_counts,
        "results_json": str(iteration_dir / "results.json"),
        "results_md": str(iteration_dir / "results.md"),
    }


def render_iterations_markdown(summaries: list[dict[str, Any]]) -> str:
    lines = [
        "# ClawBio Benchmark Iteration Summary",
        "",
        f"- Iterations: `{len(summaries)}`",
        "",
        "| Iteration | Tasks | Finished | Success | Failure | Finished Rate | Success Rate | Wall Time (s) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        lines.append(
            f"| {summary['iteration']} | {summary['task_count']} | {summary['finished_count']} | "
            f"{summary['success_count']} | {summary['failure_count']} | "
            f"{summary['finished_rate']:.2%} | {summary['success_rate']:.2%} | "
            f"{summary['wall_time_seconds']:.3f} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_single_iteration_compat_outputs(run_root_dir: Path) -> None:
    """Back-compat for consumers expecting single-run outputs at run root.

    Maps:
      run_root/results.json -> run_root/iteration-001/results.json
      run_root/results.md   -> run_root/iteration-001/results.md
      run_root/instances    -> run_root/iteration-001/instances
      run_root/logs/run.log -> run_root/run.log
    """
    iteration_dir = run_root_dir / "iteration-001"
    if not iteration_dir.exists():
        return

    links = [
        (iteration_dir / "results.json", run_root_dir / "results.json"),
        (iteration_dir / "results.md", run_root_dir / "results.md"),
        (iteration_dir / "instances", run_root_dir / "instances"),
    ]

    for src, dst in links:
        if dst.exists() or dst.is_symlink():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.symlink_to(src.name if src.parent == run_root_dir else src)

    logs_dir = run_root_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    compat_run_log = logs_dir / "run.log"
    if compat_run_log.exists() or compat_run_log.is_symlink():
        if compat_run_log.is_dir() and not compat_run_log.is_symlink():
            shutil.rmtree(compat_run_log)
        else:
            compat_run_log.unlink()
    compat_run_log.symlink_to(run_root_dir / "run.log")


def render_iterations_console_table(summaries: list[dict[str, Any]]) -> str:
    lines = [
        "All iterations summary:",
        "| Iteration | Task Count | Finished | Success | Failure | Finished Rate | Success Rate | Wall Time(s) | Avg Duration(s) | Failure Stage Counts |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for summary in summaries:
        lines.append(
            f"| {summary['iteration']} | {summary['task_count']} | {summary['finished_count']} | "
            f"{summary['success_count']} | {summary['failure_count']} | "
            f"{summary['finished_rate']:.2%} | {summary['success_rate']:.2%} | "
            f"{summary['wall_time_seconds']:.3f} | {summary['avg_duration_seconds']:.3f} | "
            f"`{summary['failure_stage_counts']}` |"
        )
    return "\n".join(lines)


# ── CLI ──


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run-benchmark.py",
        description="Run ClawBio skill tasks in parallel across Dockerized OpenClaw instances.",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=None,
        metavar="N",
        help="Number of instances to use (default: all discovered).",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_DIR / "tasks.json"),
        help="Path to the task config file (default: config/tasks.json).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(RESULTS_DIR),
        help="Benchmark output root (default: results/).",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip container health preflight checks.",
    )
    parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        default=1,
        metavar="N",
        help="Number of benchmark iterations to run (default: 1).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        metavar="ID",
        help=(
            "Run identifier (e.g. YYYYMMDD-HHMMSS). When provided, output_dir/<run-id> "
            "is used as the run root and the 'latest' symlink is not created (caller handles it)."
        ),
    )
    return parser.parse_args()


# ── Main ──


def main() -> None:
    args = parse_args()

    task_config_path = Path(args.config).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    run_id: str | None = args.run_id

    if not task_config_path.exists():
        raise SystemExit(f"Error: task config not found: {task_config_path}")

    task_config = load_task_config(task_config_path)
    defaults = task_config["defaults"]
    tasks = task_config["tasks"]

    instances = discover_instances()

    if args.instances is not None and args.instances < 1:
        raise SystemExit("Error: --instances must be at least 1.")
    if args.instances is not None and len(instances) < args.instances:
        raise SystemExit(
            f"Error: requested {args.instances} worker(s), "
            f"but only {len(instances)} instance(s) are configured."
        )
    if args.iterations < 1:
        raise SystemExit("Error: --iterations must be at least 1.")

    requested = args.instances or len(instances)

    selected_instances = (
        instances[:requested]
        if args.skip_preflight
        else ensure_prerequisites(instances, requested)
    )

    # Create run root directory
    output_dir.mkdir(parents=True, exist_ok=True)
    if run_id:
        run_root_dir = output_dir / run_id
        run_root_dir.mkdir(exist_ok=True)
        # Caller (shell script) manages 'latest' symlink when --run-id is used.
    else:
        run_root_dir = output_dir / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        run_root_dir.mkdir()
        ensure_latest_link(output_dir, run_root_dir)

    run_logger = RunLogger(run_root_dir / "run.log")

    run_logger.log(
        f"Starting ClawBio benchmark: iterations={args.iterations}, "
        f"instances={len(selected_instances)}, tasks_per_iteration={len(tasks)}, config={task_config_path}"
    )

    for instance in selected_instances:
        run_logger.log(
            f"Discovered instance {instance.index}: container={instance.container_name} "
            f"workspace={instance.workspace_dir}"
        )

    task_buckets = shard_tasks(tasks, len(selected_instances))
    iteration_summaries: list[dict[str, Any]] = []
    had_failures = False

    for iteration in range(1, args.iterations + 1):
        iteration_started_at = datetime.now(timezone.utc)
        iteration_dir = run_root_dir / f"iteration-{iteration:03d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        instances_output_dir = iteration_dir / "instances"
        instances_output_dir.mkdir(parents=True, exist_ok=True)

        run_logger.log(f"[iteration {iteration}/{args.iterations}] started: output={iteration_dir}")

        workers: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(selected_instances)) as executor:
            futures = []
            for worker_position, (instance, worker_tasks) in enumerate(
                zip(selected_instances, task_buckets), start=1
            ):
                futures.append(
                    executor.submit(
                        run_worker,
                        instance,
                        worker_position,
                        worker_tasks,
                        defaults,
                        instances_output_dir,
                        run_logger,
                    )
                )
            for future in as_completed(futures):
                workers.append(future.result())

        workers.sort(key=lambda item: item["worker"])
        payload = build_results_payload(args, task_config_path, workers, iteration_dir, iteration=iteration)
        is_complete, completion_error = validate_iteration_completion(payload, len(tasks))
        if not is_complete:
            run_logger.log(
                f"[iteration {iteration}/{args.iterations}] incomplete results: {completion_error}"
            )
            sys.exit(1)

        results_json = iteration_dir / "results.json"
        results_md = iteration_dir / "results.md"
        results_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        results_md.write_text(render_results_markdown(payload), encoding="utf-8")

        iteration_finished_at = datetime.now(timezone.utc)
        summary = build_iteration_summary(
            iteration, payload, iteration_dir, iteration_started_at, iteration_finished_at
        )
        iteration_summaries.append(summary)

        run_logger.log(
            f"[iteration {iteration}/{args.iterations}] complete: "
            f"finished={summary['finished_count']} success={summary['success_count']} "
            f"failure={summary['failure_count']} "
            f"finished_rate={summary['finished_rate']:.2%} success_rate={summary['success_rate']:.2%} "
            f"wall_time={summary['wall_time_seconds']:.3f}s"
        )
        if summary["failure_stage_counts"]:
            run_logger.log(
                f"[iteration {iteration}/{args.iterations}] failure_stages={summary['failure_stage_counts']}"
            )

        if iteration < args.iterations:
            failed_containers = {
                str(task.get("container_name"))
                for task in payload.get("tasks", [])
                if str(task.get("status", "")).strip().lower() != "success"
            }
            recover_failed_instances(selected_instances, failed_containers, run_logger)

        if payload["totals"]["failure_count"] > 0:
            had_failures = True

    iterations_json = run_root_dir / "iterations-summary.json"
    iterations_md = run_root_dir / "iterations-summary.md"
    iterations_json.write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "iterations": iteration_summaries}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    iterations_md.write_text(render_iterations_markdown(iteration_summaries), encoding="utf-8")

    if args.iterations == 1:
        write_single_iteration_compat_outputs(run_root_dir)

    total_tasks = sum(item["task_count"] for item in iteration_summaries)
    total_finished = sum(item["finished_count"] for item in iteration_summaries)
    total_success = sum(item["success_count"] for item in iteration_summaries)
    total_failure = sum(item["failure_count"] for item in iteration_summaries)
    run_logger.log(
        f"All iterations complete: iterations={args.iterations} tasks={total_tasks} "
        f"finished={total_finished} success={total_success} failure={total_failure} root={run_root_dir}"
    )
    for line in render_iterations_console_table(iteration_summaries).splitlines():
        run_logger.log(line)
    run_logger.log(f"Iteration summary: {iterations_json}")
    run_logger.log(f"Iteration markdown: {iterations_md}")

    if had_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
