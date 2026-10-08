"""Load the pinned official benchmark without changing task definitions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_URL = "https://github.com/GAIR-NLP/WindowsAgentArena-V2.git"
UPSTREAM_REVISION = "2927fe55005d1be75d5a9188c0045f73ba28d192"
MANIFEST_SHA256 = "0e3647cf34fc3460e386d392446c4eeb6e2a1250f18e04d4cf9c683ef5de34a4"
CLIENT_PATH = "src/win-arena-container/client"
COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

BENCHMARKS = {
    "waa": {"url": "https://github.com/microsoft/WindowsAgentArena.git",
            "revision": "6d39ed88c545a0d40a7a02e39b928e278df7332b",
            "manifest_sha256": "902fb24cf0a9e146bf6c29288e74a7254fe40294e49dd47363e0b15d65608fef",
            "tasks": 154, "domains": 12, "screen_size": (1920, 1080)},
    "waa-v2": {"url": UPSTREAM_URL, "revision": UPSTREAM_REVISION,
               "manifest_sha256": MANIFEST_SHA256, "tasks": 141, "domains": 12,
               "screen_size": (1280, 720)},
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Task:
    domain: str
    id: str
    path: Path
    sha256: str

    @property
    def key(self):
        return f"{self.domain}/{self.id}"


def load_tasks(client, *, benchmark="waa-v2", domains=(), task_ids=(), official=True):
    release = BENCHMARKS[benchmark]
    root = Path(client).resolve() / "evaluation_examples_windows"
    manifest_path = root / "test_all.json"
    if official and digest(manifest_path) != release["manifest_sha256"]:
        raise ValueError(f"{benchmark} task manifest differs from the pinned official release")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("Expected a nonempty domain/task manifest")
    examples = root / "examples"
    tasks = []
    seen = set()
    for domain, ids in manifest.items():
        if not COMPONENT.fullmatch(domain) or not isinstance(ids, list) or not ids:
            raise ValueError("Invalid WAA domain or task list")
        for task_id in ids:
            if not isinstance(task_id, str) or not COMPONENT.fullmatch(task_id):
                raise ValueError("Invalid WAA task ID")
            key = f"{domain}/{task_id}"
            if key in seen:
                raise ValueError(f"Duplicate task: {key}")
            seen.add(key)
            path = (examples / domain / (task_id + ".json")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"Missing WAA task: {key}")
            config = json.loads(path.read_text(encoding="utf-8"))
            # Several official files retain a different embedded ID. Native
            # WAA uses it for task-local caches; results use the manifest ID.
            if not isinstance(config.get("id"), str) or not COMPONENT.fullmatch(config["id"]):
                raise ValueError(f"Invalid embedded task ID: {key}")
            if not isinstance(config.get("instruction"), str) or not config["instruction"].strip():
                raise ValueError(f"Missing instruction: {key}")
            if not isinstance(config.get("config", []), list) or not isinstance(config.get("evaluator"), dict):
                raise TypeError(f"Missing setup/evaluator: {key}")
            tasks.append(Task(domain, task_id, path, digest(path)))
    if official and (len(tasks) != release["tasks"] or len(manifest) != release["domains"]):
        raise ValueError(f"Expected all {release['tasks']} pinned {benchmark} tasks")
    unknown_domains = set(domains) - manifest.keys()
    if unknown_domains:
        raise ValueError("Unknown WAA domains: " + ", ".join(sorted(unknown_domains)))
    selected = [task for task in tasks if not domains or task.domain in domains]
    if task_ids:
        requested = set(task_ids)
        selected = [task for task in selected if task.key in requested or task.id in requested]
        found = {name for task in selected for name in (task.key, task.id)}
        if requested - found:
            raise ValueError("Unknown or excluded WAA tasks: " + ", ".join(sorted(requested - found)))
    if not selected:
        raise ValueError("No WAA tasks selected")
    return selected
