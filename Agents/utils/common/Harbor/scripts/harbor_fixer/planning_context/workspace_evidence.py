"""Collect deterministic, bounded workspace manifests and evidence excerpts."""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any

from ..analyzer_inputs import resolve_analyzer_paths
from ..validation import ValidationError, task_key
from .safe_paths import inspect_path

MAX_TOP_LEVEL_ENTRIES = 200
MAX_MANIFESTS = 100
MAX_MANIFEST_LINES = 160
MAX_EVIDENCE_EXCERPTS = 100
MAX_EVIDENCE_LINES = 80
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_CHARS = 24_000
MAX_TARGET_CONTEXT_CHARS = 400_000
MAX_PROJECT_DEPTH = 4
MAX_SCAN_ENTRIES = 10_000
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
MANIFEST_PATTERNS = {
    "Cargo.toml",
    "Dockerfile",
    "Pipfile",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "environment.yml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "requirements*.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}
SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg"}
SENSITIVE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "token",
    "tokens.json",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"""(?i)(["']?(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|base[_-]?url|password|secret|token)(?:[_-][A-Za-z0-9]+)*["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,}]+)"""
    ),
    re.compile(r"(?<![A-Za-z0-9])(?:sk[-_]|gh[pousr]_)[A-Za-z0-9_-]{12,}"),
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
BASE64_SECRET_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"
)
URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@"
)


def _is_sensitive(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    for part in lowered_parts:
        if part in SENSITIVE_PARTS or part.startswith(".env"):
            return True
    name = path.name.lower()
    return (
        name in SENSITIVE_NAMES
        or name.endswith(tuple(SENSITIVE_SUFFIXES))
        or "private_key" in name
        or (".docker" in lowered_parts and name == "config.json")
    )


def _redact(text: str) -> str:
    redacted = URL_USERINFO_PATTERN.sub(r"\1<REDACTED>@", text)
    redacted = PRIVATE_KEY_PATTERN.sub("<REDACTED PRIVATE KEY>", redacted)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}<REDACTED>"
                if match.lastindex
                else "<REDACTED>"
            ),
            redacted,
        )
    return BASE64_SECRET_PATTERN.sub(
        lambda match: (
            "<REDACTED>"
            if any(character.isupper() for character in match.group())
            and any(character.islower() for character in match.group())
            and any(character.isdigit() for character in match.group())
            and any(character in "+/=" for character in match.group())
            else match.group()
        ),
        redacted,
    )


def _bounded(text: str) -> str:
    redacted = _redact(text)
    if len(redacted) <= MAX_OUTPUT_CHARS:
        return redacted
    return redacted[:MAX_OUTPUT_CHARS] + "\n<TRUNCATED>"


def _json_chars(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _bound_target_context(context: dict[str, Any]) -> dict[str, Any]:
    context_chars = _json_chars(context)
    for items, owner, truncated_key in (
        (
            context["workspace"]["project_manifests"],
            context["workspace"],
            "project_manifests_truncated",
        ),
        (context["evidence_excerpts"], context, "evidence_excerpts_truncated"),
    ):
        while items and context_chars > MAX_TARGET_CONTEXT_CHARS:
            removed_chars = _json_chars(items.pop())
            context_chars -= removed_chars + (1 if items else 0)
            owner[truncated_key] = True
    if _json_chars(context) > MAX_TARGET_CONTEXT_CHARS:
        raise ValidationError("target context metadata exceeds size limit")
    return context


def _path_state(path: Path) -> dict[str, Any]:
    return inspect_path(
        path,
        expand_user=False,
        include_writable=False,
        include_executable=False,
        include_mode=False,
    )


def _read_lines(path: Path, start: int, end: int) -> tuple[str, str]:
    if _is_sensitive(path):
        return "", "sensitive_path"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return "", f"path_unavailable:{exc.__class__.__name__}"
    if _is_sensitive(resolved):
        return "", "sensitive_path"
    if not resolved.is_file():
        return "", "path_is_not_file"
    try:
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return "", "file_too_large"
        with resolved.open("rb") as handle:
            if b"\x00" in handle.read(8192):
                return "", "binary_file"
        selected: list[str] = []
        with resolved.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if line_number > end:
                    break
                if line_number >= start:
                    selected.append(f"{line_number}: {line.rstrip()}")
    except OSError as exc:
        return "", f"read_failed:{exc.__class__.__name__}"
    return _bounded("\n".join(selected)), ""


def _top_level_entries(workspace: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        children = sorted(workspace.iterdir(), key=lambda path: path.name)
    except OSError:
        return entries
    for child in children:
        if child.is_symlink() or _is_sensitive(child):
            continue
        kind = "directory" if child.is_dir() else "file" if child.is_file() else "other"
        entries.append({"path": str(child), "type": kind})
        if len(entries) >= MAX_TOP_LEVEL_ENTRIES:
            break
    return entries


def _is_manifest(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in MANIFEST_PATTERNS)


def _project_manifests(workspace: Path) -> tuple[list[dict[str, Any]], bool]:
    manifests: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for current_root, directories, filenames in os.walk(workspace, followlinks=False):
        current = Path(current_root)
        depth = len(current.relative_to(workspace).parts)
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRECTORIES
            and depth < MAX_PROJECT_DEPTH
            and not (current / name).is_symlink()
            and not _is_sensitive(current / name)
        )
        scanned += len(directories)
        if scanned > MAX_SCAN_ENTRIES:
            return manifests, True
        for name in sorted(filenames):
            scanned += 1
            if scanned > MAX_SCAN_ENTRIES:
                return manifests, True
            candidate = current / name
            if candidate.is_symlink() or _is_sensitive(candidate) or not _is_manifest(candidate):
                continue
            state = _path_state(candidate)
            excerpt, error = _read_lines(candidate, 1, MAX_MANIFEST_LINES)
            state["excerpt"] = excerpt
            state["status"] = "success" if not error else "unavailable"
            state["reason"] = error
            manifests.append(state)
            if len(manifests) >= MAX_MANIFESTS:
                truncated = True
                return manifests, truncated
    return manifests, truncated


def _evidence_excerpts(task_inputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    excerpts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int, int]] = set()
    truncated = False
    for task_input in task_inputs:
        task = task_input["task"]
        for evidence in task_input["evidence"]:
            path_value = evidence["path"]
            requested_start = max(1, evidence["line_start"])
            requested_end = max(requested_start, evidence["line_end"])
            start = max(1, requested_start - 3)
            end = min(
                max(requested_end + 3, start),
                start + MAX_EVIDENCE_LINES - 1,
            )
            key = (*task_key(task), path_value, start, end)
            if key in seen:
                continue
            seen.add(key)
            path = Path(path_value)
            excerpt, error = _read_lines(path, start, end)
            entry: dict[str, Any] = {
                "task": task,
                "path": path_value,
                "line_start": start,
                "line_end": end,
                "status": "success" if not error else "unavailable",
                "reason": error,
                "excerpt": excerpt,
            }
            excerpts.append(entry)
            if len(excerpts) >= MAX_EVIDENCE_EXCERPTS:
                truncated = True
                return excerpts, truncated
    return excerpts, truncated


def collect_workspace_evidence(
    workspace_root: Path,
    analyzer_output_path: Path,
    task_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collect deterministic project and evidence context without model involvement."""

    workspace = workspace_root.expanduser().resolve(strict=True)
    analyzer_output = analyzer_output_path.expanduser().resolve(strict=True)
    manifests, manifests_truncated = _project_manifests(workspace)
    evidence, evidence_truncated = _evidence_excerpts(task_inputs)
    resolved_analyzer = resolve_analyzer_paths(analyzer_output)
    analyzer_artifacts = {
        "manifest": _path_state(resolved_analyzer["manifest_path"]),
        "publications": [
            {
                "handover_id": publication["handover_id"],
                "publication_id": publication["publication_id"],
                "env_infra_tasks": _path_state(Path(publication["env_infra_tasks_path"])),
                "fix_line_index": _path_state(Path(publication["fix_line_index_path"])),
            }
            for publication in resolved_analyzer["publications"]
        ],
    }
    context = {
        "schema_version": 1,
        "kind": "harbor_fixer_target_context",
        "workspace": {
            "root": str(workspace),
            "top_level_entries": _top_level_entries(workspace),
            "project_manifests": manifests,
            "project_manifests_truncated": manifests_truncated,
        },
        "analyzer_artifacts": analyzer_artifacts,
        "evidence_excerpts": evidence,
        "evidence_excerpts_truncated": evidence_truncated,
        "collection_limits": {
            "max_top_level_entries": MAX_TOP_LEVEL_ENTRIES,
            "max_project_depth": MAX_PROJECT_DEPTH,
            "max_manifests": MAX_MANIFESTS,
            "max_manifest_lines": MAX_MANIFEST_LINES,
            "max_evidence_excerpts": MAX_EVIDENCE_EXCERPTS,
            "max_evidence_lines": MAX_EVIDENCE_LINES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_target_context_chars": MAX_TARGET_CONTEXT_CHARS,
        },
    }
    return _bound_target_context(context)
