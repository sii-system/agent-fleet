"""Collect a read-only inventory of runtimes and tools available to Fixer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..validation import ValidationError
from .safe_paths import inspect_path

COMMANDS = (
    "bash",
    "sh",
    "find",
    "grep",
    "sed",
    "awk",
    "stat",
    "tar",
    "curl",
    "wget",
    "jq",
    "python3",
    "python3.12",
    "git",
    "git-lfs",
    "zellij",
    "node",
    "npm",
    "npx",
    "pi",
    "opik",
    "harbor",
)

VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "git": ("--version",),
    "git-lfs": ("version",),
    "zellij": ("--version",),
    "node": ("--version",),
    "npm": ("--version",),
    "npx": ("--version",),
    "pi": ("--version",),
    "opik": ("--version",),
    "harbor": ("--version",),
}

PYTHON_MODULES = ("harbor", "opik", "pydantic", "uuid6", "socksio")
MAX_EVIDENCE_PATHS = 500
PROBE_TIMEOUT_SECONDS = 5


def _path_state(path: Path) -> dict[str, Any]:
    return inspect_path(
        path,
        expand_user=True,
        include_writable=True,
        include_executable=True,
        include_mode=True,
    )


def _run_probe(argv: list[str], *, timeout: int = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "succeeded": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    result: dict[str, Any] = {
        "succeeded": completed.returncode == 0,
        "exit_code": completed.returncode,
    }
    if stdout:
        result["stdout"] = stdout[:2000]
    if stderr:
        result["stderr"] = stderr[:2000]
    return result


def _find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _command_snapshot(pi_bin: str | None = None) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for name in COMMANDS:
        configured = pi_bin if name == "pi" and pi_bin else name
        resolved = shutil.which(configured)
        item: dict[str, Any] = {
            "available": resolved is not None,
            "path": resolved or "",
        }
        args = VERSION_ARGS.get(name)
        if resolved and args:
            item["version_probe"] = _run_probe([resolved, *args])
        snapshot[name] = item
    return snapshot


def _python_snapshot(commands: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        Path(sys.executable),
        *(Path(commands[name]["path"]) for name in ("python3", "python3.12") if commands[name]["path"]),
    ]
    module_script = "\n".join(
        [
            "import importlib.metadata as metadata",
            "import importlib.util as util",
            "import json",
            "import sys",
            f"names = {PYTHON_MODULES!r}",
            "def package_version(name):",
            "    try:",
            "        return metadata.version(name)",
            "    except metadata.PackageNotFoundError:",
            "        return None",
            "modules = {}",
            "for name in names:",
            "    spec = util.find_spec(name)",
            "    modules[name] = {",
            "        'available': spec is not None,",
            "        'path': getattr(spec, 'origin', None),",
            "        'version': package_version(name),",
            "    }",
            "print(json.dumps({",
            "    'executable': sys.executable,",
            "    'version': sys.version.split()[0],",
            "    'modules': modules,",
            "}))",
        ]
    )
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.expanduser().absolute()
        if not candidate.exists() or not os.access(candidate, os.X_OK):
            continue
        executable = str(candidate)
        if executable in seen:
            continue
        seen.add(executable)
        probe = _run_probe([executable, "-c", module_script])
        parsed: dict[str, Any] | None = None
        if probe.get("succeeded") and isinstance(probe.get("stdout"), str):
            try:
                value = json.loads(probe["stdout"])
                parsed = value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                parsed = None
        snapshots.append(
            parsed
            or {
                "executable": executable,
                "probe_error": probe,
            }
        )
    return snapshots


def _uv_snapshot() -> dict[str, Any]:
    uv = shutil.which("uv")
    snapshot: dict[str, Any] = {
        "cli": {
            "available": uv is not None,
            "path": uv or "",
        }
    }
    if uv:
        snapshot["tool_dir_probe"] = _run_probe([uv, "tool", "dir"])
        snapshot["cache_dir_probe"] = _run_probe([uv, "cache", "dir"])
    return snapshot


def _docker_snapshot() -> dict[str, Any]:
    docker = shutil.which("docker")
    docker_host = os.environ.get("DOCKER_HOST", "")
    socket = (
        _path_state(Path(docker_host.removeprefix("unix://") or "/var/run/docker.sock"))
        if not docker_host or docker_host.startswith("unix://")
        else {
            "path": "",
            "exists": False,
            "note": "non-unix DOCKER_HOST is configured; endpoint value omitted",
        }
    )
    snapshot: dict[str, Any] = {
        "cli": {
            "available": docker is not None,
            "path": docker or "",
        },
        "socket": socket,
    }
    if docker:
        snapshot.update(
            {
                "compose_probe": _run_probe([docker, "compose", "version"]),
                "daemon_probe": _run_probe(
                    [
                        docker,
                        "info",
                        "--format",
                        (
                            "server_version={{.ServerVersion}}\n"
                            "containers={{.Containers}}\n"
                            "images={{.Images}}\n"
                            "driver={{.Driver}}\n"
                            "operating_system={{.OperatingSystem}}\n"
                            "architecture={{.Architecture}}"
                        ),
                    ]
                ),
            }
        )
    return snapshot


def _evidence_paths(task_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    paths: list[str] = []
    seen: set[str] = set()
    for task_input in task_inputs:
        for evidence in task_input["evidence"]:
            value = evidence["path"]
            if value in seen:
                continue
            seen.add(value)
            paths.append(value)
    selected = paths[:MAX_EVIDENCE_PATHS]
    return {
        "total_count": len(paths),
        "truncated": len(paths) > len(selected),
        "paths": [_path_state(Path(value)) for value in selected],
    }


def collect_runtime_inventory(
    workspace_root: Path,
    task_inputs: list[dict[str, Any]],
    *,
    pi_bin: str | None = None,
) -> dict[str, Any]:
    """Collect a bounded, non-secret snapshot without modifying the target environment."""

    try:
        workspace = workspace_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"workspace root is missing or unreadable: {workspace_root}") from exc
    if not workspace.is_dir() or not os.access(workspace, os.R_OK):
        raise ValidationError(f"workspace root must be a readable directory: {workspace_root}")

    harbor_root = Path(__file__).resolve().parents[3]
    repo_root = _find_repo_root(harbor_root)
    commands = _command_snapshot(pi_bin)
    cache_root = Path(
        os.environ.get(
            "AGENT_FLEET_CACHE_DIR",
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "agent-fleet",
        )
    )
    local_dependency_cache = Path(
        os.environ.get("LOCAL_WHEEL_DIR", cache_root / "harbor-deps")
    )
    repository_paths: dict[str, dict[str, Any]] = {
        "workspace_root": _path_state(workspace),
        "harbor_common": _path_state(harbor_root),
        "local_dependency_cache": _path_state(local_dependency_cache),
    }
    if repo_root is not None:
        repository_paths.update(
            {
                "repository_root": _path_state(repo_root),
                "tasks": _path_state(repo_root / "Tasks"),
                "claude_code_adapter": _path_state(repo_root / "Agents/Harbor-claude-code"),
                "opencode_adapter": _path_state(repo_root / "Agents/Harbor-opencode"),
                "opik_plugin": _path_state(repo_root / "third_party/agent-opik-plugin"),
            }
        )

    return {
        "schema_version": 1,
        "kind": "harbor_fixer_target_environment",
        "repository_paths": repository_paths,
        "commands": commands,
        "python_runtimes": _python_snapshot(commands),
        "uv": _uv_snapshot(),
        "docker": _docker_snapshot(),
        "evidence_files": _evidence_paths(task_inputs),
    }
