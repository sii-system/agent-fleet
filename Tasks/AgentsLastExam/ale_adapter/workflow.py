"""Prepared host environment and exact native task selection."""

import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_packages(python):
    return json.loads(subprocess.check_output([
        str(python), "-c", ("import importlib.metadata,json;print(json.dumps({"
        "d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))"),
    ], text=True))


def environment_marker(env_dir, lockfile):
    marker = json.loads((Path(env_dir) / ".agent-fleet-ale.json").read_text())
    if (sys.version_info[:2] != (3, 12) or marker.get("version") != 1
            or marker.get("lock_sha256") != digest(lockfile) or not marker.get("packages")):
        raise ValueError("ALE host environment is stale; run Tasks/AgentsLastExam/setup.sh")
    for name, version in marker["packages"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError("ALE host packages changed; run Tasks/AgentsLastExam/setup.sh")
    if native_packages(marker["native_python"]) != marker["native_packages"]:
        raise ValueError("ALE native packages changed; run Tasks/AgentsLastExam/setup.sh")
    return marker


def select_tasks(definitions, *, domains=(), task_ids=(), os_type=None):
    records = [(path, json.loads(path.read_text())) for path in definitions]
    available_domains = {Path(native["task"]).parts[1] for _, native in records}
    if unknown := set(domains) - available_domains:
        raise ValueError(f"Unknown ALE domains: {sorted(unknown)}")
    requested = {name.strip() for item in task_ids for name in item.split(",") if name.strip()}
    matched = set()
    selected = []
    for path, native in records:
        domain, task = Path(native["task"]).parts[1:]
        aliases = {path.parent.parent.name, task, f"{domain}/{task}"}
        matches = requested & aliases
        if domains and domain not in domains or os_type and native["os"] != os_type:
            continue
        if requested and not matches:
            continue
        matched.update(matches)
        selected.append(path)
    if requested - matched:
        raise ValueError(f"Unknown or filtered ALE tasks: {sorted(requested - matched)}")
    if not selected:
        raise ValueError("No ALE CPU tasks selected")
    return selected
