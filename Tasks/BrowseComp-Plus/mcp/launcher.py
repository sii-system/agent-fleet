#!/usr/bin/env python3
"""Lifecycle manager for one shared host-side BrowseComp MCP retriever."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_DIR / "scripts"))
sys.path.insert(0, str(BENCHMARK_DIR))
from common import default_cache_root, default_source_root  # noqa: E402
from retriever.config import RetrieverConfig  # noqa: E402

PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def retriever_environment() -> dict[str, str]:
    env = os.environ.copy()
    backend = env.get("BROWSECOMP_EMBEDDING_BACKEND", "local").strip().lower()
    api_proxy_mode = env.get(
        "BROWSECOMP_EMBEDDING_PROXY_MODE", "direct"
    ).strip().lower()
    if backend == "openai" and api_proxy_mode not in {"direct", "inherit"}:
        raise ValueError(
            "BROWSECOMP_EMBEDDING_PROXY_MODE must be direct or inherit"
        )
    if env.get("BROWSECOMP_HF_PROXY_MODE_RESOLVED") == "direct" and (
        backend != "openai" or api_proxy_mode == "direct"
    ):
        for name in PROXY_VARIABLES:
            env.pop(name, None)
    if backend == "openai" and api_proxy_mode == "direct":
        host = urlparse(env.get("BROWSECOMP_EMBEDDING_BASE_URL", "")).hostname
        if host:
            existing = env.get("NO_PROXY", env.get("no_proxy", ""))
            values = [value.strip() for value in existing.split(",") if value.strip()]
            if host not in values:
                values.append(host)
            env["NO_PROXY"] = ",".join(values)
            env["no_proxy"] = env["NO_PROXY"]
    return env


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    return not cmdline.exists() or "BrowseComp-Plus/mcp/server.py" in cmdline.read_text(errors="ignore")


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_command(path: Path) -> list[str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, list) and all(
        isinstance(value, str) for value in payload
    ) else None


def command_option(command: list[str], option: str) -> str | None:
    try:
        index = command.index(option)
    except ValueError:
        return None
    return command[index + 1] if index + 1 < len(command) else None


def remove_option(command: list[str], option: str, value: str) -> list[str] | None:
    try:
        index = command.index(option)
    except ValueError:
        return None
    if index + 1 >= len(command) or command[index + 1] != value:
        return None
    return command[:index] + command[index + 2 :]


def normalize_managed_port(command: list[str]) -> list[str] | None:
    """Replace the launcher-selected port with a stable comparison token."""

    normalized: list[str] = []
    index = 0
    while index < len(command):
        value = command[index]
        if value == "--port":
            if index + 1 >= len(command):
                return None
            normalized.extend(("--port", "<managed-port>"))
            index += 2
            continue
        if value.startswith("--port="):
            normalized.append("--port=<managed-port>")
        else:
            normalized.append(value)
        index += 1
    return normalized


def commands_equivalent(
    existing: list[str] | None, requested: list[str]
) -> bool:
    """Ignore the managed port and accept the pre-API local defaults.

    This permits an automatically shifted warm MCP process to be reused and an
    already-managed local process to survive the feature upgrade, without
    concealing an actual local-vs-remote or model configuration change.
    """

    if not isinstance(existing, list):
        return False
    existing_normalized = normalize_managed_port(existing)
    requested_normalized = normalize_managed_port(requested)
    if existing_normalized is None or requested_normalized is None:
        return False
    if existing_normalized == requested_normalized:
        return True
    if command_option(requested_normalized, "--embedding-backend") != "local":
        return False
    model = command_option(requested_normalized, "--model-name")
    if not model:
        return False
    defaults = [
        ("--embedding-backend", "local"),
        ("--embedding-api-key-env", "API_KEY"),
        ("--embedding-api-model", model),
        ("--embedding-api-timeout-seconds", "60.0"),
        ("--embedding-api-max-retries", "2"),
        ("--tokenizer-model", model),
    ]
    revision = command_option(requested_normalized, "--model-revision")
    if revision:
        defaults.append(("--tokenizer-revision", revision))
    legacy = requested_normalized
    for option, value in defaults:
        legacy = remove_option(legacy, option, value)
        if legacy is None:
            return False
    return existing_normalized == legacy


def probe(url: str, timeout: float = 2.0) -> bool:
    # An MCP endpoint commonly answers an unauthenticated GET with 400/405/406;
    # any HTTP response proves the listener is ready.
    request = urllib.request.Request(url, headers={"Accept": "application/json, text/event-stream"})
    # Host health checks must never be routed through HTTP_PROXY. In particular,
    # an empty NO_PROXY can make an unrelated proxy response look like a ready
    # local MCP process.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError):
        return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_default_port(
    state_dir: Path, start_port: int = 8000, requested_command: list[str] | None = None
) -> int:
    selected_file = state_dir / "default-port"
    candidates: list[int] = []
    try:
        persisted = int(selected_file.read_text(encoding="utf-8").strip())
        if 1 <= persisted <= 65535:
            candidates.append(persisted)
    except (OSError, ValueError):
        pass
    candidates.extend(range(start_port, min(start_port + 1000, 65536)))
    unique_candidates = list(dict.fromkeys(candidates))
    occupied_by_other_retriever: set[int] = set()
    for port in unique_candidates:
        pid = read_pid(state_dir / f"mcp-{port}.pid")
        command = read_command(state_dir / f"mcp-{port}.command.json")
        if pid and is_alive(pid):
            if requested_command is None or commands_equivalent(
                command, requested_command
            ):
                selected_file.write_text(f"{port}\n", encoding="utf-8")
                return port
            occupied_by_other_retriever.add(port)
    for port in unique_candidates:
        if port in occupied_by_other_retriever:
            continue
        if port_available(port):
            selected_file.write_text(f"{port}\n", encoding="utf-8")
            return port
    raise RuntimeError(f"no available BrowseComp MCP port from {start_port}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["command", "resolve-port", "start", "health", "stop"]
    )
    parser.add_argument("--source-root", type=Path, default=default_source_root())
    parser.add_argument("--state-dir", type=Path, default=default_cache_root() / "mcp")
    parser.add_argument("--wait-seconds", type=float, default=180)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = RetrieverConfig.from_env(args.source_root)
    state_dir = args.state_dir.resolve()
    requested_command = (
        config.command()
        if args.action in {"command", "resolve-port", "start"}
        else None
    )
    if args.action == "command":
        print(json.dumps(requested_command))
        return 0
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = (state_dir / "launcher.lock").open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    if args.action == "resolve-port":
        if "BROWSECOMP_MCP_PORT" in os.environ:
            print(config.port)
        else:
            print(choose_default_port(state_dir, config.port, requested_command))
        return 0
    if args.action == "start" and "BROWSECOMP_MCP_PORT" not in os.environ:
        selected_port = choose_default_port(state_dir, config.port, requested_command)
        if selected_port != config.port:
            config = replace(config, port=selected_port)
            requested_command = config.command()
    pid_file = state_dir / f"mcp-{config.port}.pid"
    log_file = state_dir / f"mcp-{config.port}.log"
    command_file = state_dir / f"mcp-{config.port}.command.json"
    pid = read_pid(pid_file)

    if args.action == "health":
        healthy = bool(pid and is_alive(pid) and probe(config.local_url))
        print(json.dumps({"healthy": healthy, "pid": pid, "local_url": config.local_url, "public_url": config.public_url}))
        return 0 if healthy else 1
    if args.action == "stop":
        if pid and is_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not is_alive(pid):
                    break
                time.sleep(0.1)
            if is_alive(pid):
                raise TimeoutError(f"BrowseComp MCP pid {pid} did not stop; state files were retained")
        pid_file.unlink(missing_ok=True)
        command_file.unlink(missing_ok=True)
        print(f"BrowseComp MCP stopped: {pid or 'not running'}")
        return 0

    existing_command = read_command(command_file)
    if not (pid and is_alive(pid)) and probe(config.local_url):
        raise RuntimeError(
            f"port {config.port} is already occupied by an unmanaged service; "
            "unset BROWSECOMP_MCP_PORT to select one automatically"
        )
    commands_match = commands_equivalent(existing_command, requested_command)
    if pid and is_alive(pid) and probe(config.local_url) and not commands_match:
        raise RuntimeError(
            f"BrowseComp MCP on port {config.port} uses a different retriever configuration; stop it or choose another port"
        )
    if pid and is_alive(pid) and probe(config.local_url):
        if existing_command != requested_command:
            command_file.write_text(
                json.dumps(requested_command, indent=2) + "\n", encoding="utf-8"
            )
        payload = {"started": False, "pid": pid, "local_url": config.local_url, "public_url": config.public_url, "log": str(log_file)}
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
        with log_file.open("ab") as log:
            process = subprocess.Popen(
                requested_command,
                cwd=BENCHMARK_DIR,
                env=retriever_environment(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
        command_file.write_text(json.dumps(requested_command, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + args.wait_seconds
        next_progress = time.monotonic() + 15
        print(
            "[BrowseComp] loading retriever; first start downloads corpus and "
            "tokenizer (the local backend also downloads embedding weights) "
            f"(log: {log_file})",
            flush=True,
        )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pid_file.unlink(missing_ok=True)
                command_file.unlink(missing_ok=True)
                raise RuntimeError(f"BrowseComp MCP exited with {process.returncode}; see {log_file}")
            if probe(config.local_url):
                time.sleep(0.25)
                if process.poll() is None:
                    break
            if time.monotonic() >= next_progress:
                print(f"[BrowseComp] retriever is still loading (log: {log_file})", flush=True)
                next_progress = time.monotonic() + 15
            time.sleep(0.5)
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise TimeoutError(f"BrowseComp MCP startup timed out and pid {process.pid} did not stop; see {log_file}")
            pid_file.unlink(missing_ok=True)
            command_file.unlink(missing_ok=True)
            raise TimeoutError(f"BrowseComp MCP did not become ready; see {log_file}")
        payload = {"started": True, "pid": process.pid, "local_url": config.local_url, "public_url": config.public_url, "log": str(log_file)}
    if args.json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(f"BrowseComp MCP ready: {payload['public_url']} (pid {payload['pid']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
