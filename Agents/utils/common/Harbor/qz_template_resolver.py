"""Resolve and materialize per-task QZ Templates from a mapping inventory."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import qz_template_manager as manager
import qz_template_mapping as mapping

MAPPING_ENV_VAR = "QZ_SANDBOX_TEMPLATE_MAP"
SUPPORTED_MAPPING_VERSIONS = {
    (mapping.LEGACY_SCHEMA_VERSION, mapping.LEGACY_IDENTITY_VERSION),
    (mapping.PREVIOUS_SCHEMA_VERSION, mapping.IDENTITY_VERSION),
    (mapping.SCHEMA_VERSION, mapping.IDENTITY_VERSION),
}


class QzTemplateResolutionError(RuntimeError):
    """Raised when a task cannot resolve to one ready QZ Template."""


@dataclass(frozen=True)
class ResolvedTaskEnvironment:
    """A ready Template plus commands required for this task's fresh Sandbox."""

    template_id: str
    init_steps: tuple[dict[str, str], ...]
    workdir: str | None


def load_mapping(path: Path) -> dict[str, Any]:
    """Load and minimally validate a supported QZ Template mapping."""
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QzTemplateResolutionError(
            f"mapping is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise QzTemplateResolutionError("QZ Template mapping must be a JSON object")
    version_pair = (
        payload.get("schema_version"),
        payload.get("identity_version"),
    )
    if version_pair not in SUPPORTED_MAPPING_VERSIONS:
        raise QzTemplateResolutionError(
            "unsupported QZ Template mapping version pair: "
            f"schema_version={version_pair[0]!r}, "
            f"identity_version={version_pair[1]!r}"
        )
    if not isinstance(payload.get("tasks"), dict):
        raise QzTemplateResolutionError("QZ Template mapping is missing tasks")
    if not isinstance(payload.get("templates"), dict):
        raise QzTemplateResolutionError("QZ Template mapping is missing templates")
    return payload


def resolve_task_key(payload: Mapping[str, Any], task_name: str) -> str:
    """Return the canonical mapping key for one Harbor task name."""
    tasks = payload["tasks"]
    name = task_name.strip().strip("/")
    if name in tasks:
        return name

    suffix_matches = [
        key for key in tasks if isinstance(key, str) and name.endswith(f"/{key}")
    ]
    if suffix_matches:
        longest_length = max(map(len, suffix_matches))
        longest_matches = [key for key in suffix_matches if len(key) == longest_length]
        if len(longest_matches) == 1:
            return longest_matches[0]
        raise QzTemplateResolutionError(
            f"task {task_name!r} ambiguously matches mapping keys: "
            + ", ".join(repr(key) for key in sorted(longest_matches))
        )

    basename = name.rsplit("/", 1)[-1]
    basename_matches = [
        key
        for key in tasks
        if isinstance(key, str) and key.rsplit("/", 1)[-1] == basename
    ]
    if len(basename_matches) == 1:
        return basename_matches[0]
    if len(basename_matches) > 1:
        raise QzTemplateResolutionError(
            f"task {task_name!r} ambiguously matches mapping keys: "
            + ", ".join(repr(key) for key in sorted(basename_matches))
        )
    raise QzTemplateResolutionError(
        f"task {task_name!r} is not present in the QZ Template mapping"
    )


def task_instruction_prefix(payload: Mapping[str, Any], task_name: str) -> str:
    """Return exact setup text removed from a schema-v3 task instruction."""
    if payload.get("schema_version") != mapping.SCHEMA_VERSION:
        return ""
    key = resolve_task_key(payload, task_name)
    task = payload["tasks"].get(key)
    if not isinstance(task, dict):
        raise QzTemplateResolutionError(f"mapping task {key!r} must be an object")
    try:
        return mapping.normalize_instruction_prefix(
            task.get("instruction_prefix", "")
        )
    except mapping.QzTemplateMappingError as exc:
        raise QzTemplateResolutionError(
            f"mapping task {key!r} has an invalid instruction_prefix: {exc}"
        ) from exc


def _required_string(
    payload: Mapping[str, Any],
    key: str,
    context: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QzTemplateResolutionError(f"{context} is missing {key}")
    return value.strip()


def _normalized_plan_steps(
    payload: Mapping[str, Any],
    task: Mapping[str, Any],
    template: Mapping[str, Any],
    context: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if payload.get("schema_version") == mapping.LEGACY_SCHEMA_VERSION:
        return [], []
    try:
        build_steps = mapping.normalize_build_steps(template.get("build_steps", []))
        init_steps = mapping.normalize_init_steps(task.get("init_steps", []))
    except mapping.QzTemplateMappingError as exc:
        raise QzTemplateResolutionError(
            f"{context} has an invalid plan: {exc}"
        ) from exc
    return build_steps, init_steps


def _task_environment_entries(
    payload: Mapping[str, Any],
    task_name: str,
) -> tuple[str, dict[str, Any], list[dict[str, str]], str | None]:
    """Return validated Template and init entries selected for one task."""
    key = resolve_task_key(payload, task_name)
    task = payload["tasks"].get(key)
    if not isinstance(task, dict):
        raise QzTemplateResolutionError(f"mapping task {key!r} must be an object")
    template_key = _required_string(task, "template_key", f"mapping task {key!r}")
    task_image = _required_string(task, "docker_image", f"mapping task {key!r}")
    template = payload["templates"].get(template_key)
    if not isinstance(template, dict):
        raise QzTemplateResolutionError(
            f"mapping task {key!r} references missing Template {template_key!r}"
        )
    image = _required_string(template, "image", f"mapping Template {template_key!r}")
    image_source = _required_string(
        template,
        "image_source",
        f"mapping Template {template_key!r}",
    )
    spec = _required_string(template, "spec", f"mapping Template {template_key!r}")
    template_name = _required_string(
        template,
        "template_name",
        f"mapping Template {template_key!r}",
    )
    if spec not in mapping.SPEC_CHOICES:
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} has unsupported spec {spec!r}"
        )
    if (
        len(template_name) > mapping.TEMPLATE_NAME_MAX_LENGTH
        or mapping.TEMPLATE_NAME_PATTERN.fullmatch(template_name) is None
    ):
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} has invalid template_name"
        )
    if task_image != image:
        raise QzTemplateResolutionError(
            f"mapping task {key!r} image does not match Template {template_key!r}"
        )
    build_steps, init_steps = _normalized_plan_steps(
        payload,
        task,
        template,
        f"mapping task {key!r}",
    )
    task_instruction_prefix(payload, key)
    try:
        workdir = (
            mapping.normalize_workdir(task.get("workdir"))
            if payload.get("schema_version") == mapping.SCHEMA_VERSION
            else None
        )
    except mapping.QzTemplateMappingError as exc:
        raise QzTemplateResolutionError(
            f"mapping task {key!r} has an invalid workdir: {exc}"
        ) from exc
    identity = mapping.template_identity(
        image,
        spec,
        image_source,
        build_steps,
        identity_version=_required_string(
            payload,
            "identity_version",
            "QZ Template mapping",
        ),
    )
    expected_key = f"sha256:{identity}"
    if template_key != expected_key:
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} does not match its content identity"
        )
    expected_name = mapping.template_name(image, identity)
    if template_name != expected_name:
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} does not use its "
            "content-derived template_name"
        )
    return template_key, template, init_steps, workdir


def task_template_entry(
    payload: Mapping[str, Any],
    task_name: str,
) -> tuple[str, dict[str, Any]]:
    """Return the template key and entry selected for one Harbor task."""
    template_key, template, _, _ = _task_environment_entries(payload, task_name)
    return template_key, template


def _validated_ready_template_id(
    template: Mapping[str, Any],
    entry: Mapping[str, Any],
    context: str,
) -> str:
    """Validate the live identity fields exposed by the QZ Template API."""
    template_id = _required_string(template, "templateID", context)
    status, latest_build = manager._latest_build_state(template)
    if status != "ready":
        raise QzTemplateResolutionError(
            f"{context} is not ready (templateID={template_id}, status={status!r})"
        )
    expected_name = _required_string(entry, "template_name", context)
    names = template.get("names")
    live_names = (
        {name.strip() for name in names if isinstance(name, str) and name.strip()}
        if isinstance(names, list)
        else set()
    )
    if expected_name not in live_names:
        raise QzTemplateResolutionError(
            f"{context} does not have expected content-derived alias "
            f"{expected_name!r} (templateID={template_id})"
        )

    if latest_build is None:
        raise QzTemplateResolutionError(
            f"{context} has no live build metadata (templateID={template_id})"
        )
    expected_spec = _required_string(entry, "spec", context)
    live_spec = _required_string(
        latest_build,
        "sbxSpecCode",
        f"{context} latest build",
    )
    if live_spec != expected_spec:
        raise QzTemplateResolutionError(
            f"{context} has spec {live_spec!r}, expected {expected_spec!r} "
            f"(templateID={template_id})"
        )
    return template_id


def _resolve_template_entry(
    template_key: str,
    entry: Mapping[str, Any],
    client: manager.QzTemplateClient,
) -> str:
    cached_id = entry.get("template_id")
    if cached_id is not None:
        if not isinstance(cached_id, str) or not cached_id.strip():
            raise QzTemplateResolutionError(
                f"mapping Template {template_key!r} has an invalid template_id"
            )
        template = client.get_template(cached_id.strip())
        resolved_id = _validated_ready_template_id(
            template,
            entry,
            f"mapped Template {template_key!r}",
        )
        if resolved_id != cached_id.strip():
            raise QzTemplateResolutionError(
                f"mapped Template {template_key!r} returned ID {resolved_id!r} "
                f"for requested ID {cached_id.strip()!r}"
            )
        return resolved_id

    template_name = _required_string(
        entry,
        "template_name",
        f"mapping Template {template_key!r}",
    )
    template = client.get_by_name(template_name)
    if template is None:
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} has no template_id and "
            f"alias {template_name!r} does not exist; materialize or bind it first"
        )
    return _validated_ready_template_id(
        template,
        entry,
        f"mapped Template alias {template_name!r}",
    )


def resolve_task_template(
    mapping_path: Path,
    task_name: str,
    client: manager.QzTemplateClient,
) -> str:
    """Resolve one task to a live, ready Template ID without creating it."""
    payload = load_mapping(mapping_path)
    template_key, entry = task_template_entry(payload, task_name)
    return _resolve_template_entry(template_key, entry, client)


def resolve_task_environment(
    mapping_path: Path,
    task_name: str,
    client: manager.QzTemplateClient,
) -> ResolvedTaskEnvironment:
    """Resolve the ready Template and per-Sandbox init steps for one task."""
    payload = load_mapping(mapping_path)
    template_key, entry, init_steps, workdir = _task_environment_entries(
        payload,
        task_name,
    )
    return ResolvedTaskEnvironment(
        template_id=_resolve_template_entry(template_key, entry, client),
        init_steps=tuple(init_steps),
        workdir=workdir,
    )


def resolve_task_environment_from_environment(
    mapping_path: Path,
    task_name: str,
) -> ResolvedTaskEnvironment:
    """Resolve a task environment using credentials held only in the process."""
    try:
        return resolve_task_environment(
            mapping_path,
            task_name,
            manager.client_from_environment(),
        )
    except manager.QzTemplateError as exc:
        raise QzTemplateResolutionError(
            f"live QZ Template lookup failed for task {task_name!r}: {exc}"
        ) from exc


def _write_mapping(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def record_template_id(
    mapping_path: Path,
    template_key: str,
    template_id: str,
) -> None:
    payload = load_mapping(mapping_path)
    template = payload["templates"].get(template_key)
    if not isinstance(template, dict):
        raise QzTemplateResolutionError(
            f"mapping changed while resolving Template {template_key!r}"
        )
    current_id = template.get("template_id")
    if current_id not in (None, template_id):
        raise QzTemplateResolutionError(
            f"mapping Template {template_key!r} is already bound to "
            f"{current_id!r}; refusing to replace it"
        )
    template["template_id"] = template_id
    _write_mapping(mapping_path, payload)


def bind_task_template(
    mapping_path: Path,
    task_name: str,
    template_id: str,
    client: manager.QzTemplateClient,
) -> str:
    """Bind an existing ready Template ID to a task's mapping entry."""
    payload = load_mapping(mapping_path)
    template_key, entry = task_template_entry(payload, task_name)
    requested_id = template_id.strip()
    if not requested_id:
        raise QzTemplateResolutionError("template ID must not be empty")
    resolved_id = _validated_ready_template_id(
        client.get_template(requested_id),
        entry,
        f"Template requested for task {task_name!r}",
    )
    if resolved_id != requested_id:
        raise QzTemplateResolutionError(
            f"Template API returned {resolved_id!r} for requested ID {requested_id!r}"
        )
    record_template_id(mapping_path, template_key, resolved_id)
    return resolved_id


def materialize_template_entry(
    template_key: str,
    entry: Mapping[str, Any],
    client: manager.QzTemplateClient,
    *,
    timeout: float,
    stderr: TextIO = sys.stderr,
) -> str:
    """Create or reuse one validated mapping entry without writing the mapping."""
    cached_id = entry.get("template_id")
    if cached_id is not None:
        if not isinstance(cached_id, str) or not cached_id.strip():
            raise QzTemplateResolutionError(
                f"mapping Template {template_key!r} has an invalid template_id"
            )
        requested_id = cached_id.strip()
        ready_id = _validated_ready_template_id(
            client.get_template(requested_id),
            entry,
            f"mapped Template {template_key!r}",
        )
        if ready_id != requested_id:
            raise QzTemplateResolutionError(
                f"mapped Template {template_key!r} returned ID {ready_id!r} "
                f"for requested ID {requested_id!r}"
            )
        return ready_id

    template_id = manager.create_template_from_image(
        client,
        name=_required_string(entry, "template_name", template_key),
        image=_required_string(entry, "image", template_key),
        spec=_required_string(entry, "spec", template_key),
        image_source=_required_string(entry, "image_source", template_key),
        build_steps=entry.get("build_steps", []),
        timeout=timeout,
        exists_ok=True,
        stderr=stderr,
    )
    ready_id = _validated_ready_template_id(
        client.get_template(template_id),
        entry,
        f"materialized Template {template_key!r}",
    )
    if ready_id != template_id:
        raise QzTemplateResolutionError(
            f"Template API returned {ready_id!r} for materialized ID {template_id!r}"
        )
    return ready_id


def materialize_task_template(
    mapping_path: Path,
    task_name: str,
    client: manager.QzTemplateClient,
    *,
    timeout: float,
    stderr: TextIO = sys.stderr,
) -> str:
    """Create or reuse one mapped Template, then persist its ready ID."""
    payload = load_mapping(mapping_path)
    template_key, entry = task_template_entry(payload, task_name)
    ready_id = materialize_template_entry(
        template_key,
        entry,
        client,
        timeout=timeout,
        stderr=stderr,
    )
    record_template_id(mapping_path, template_key, ready_id)
    return ready_id


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve per-task QZ Templates from a mapping inventory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_task_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--mapping", required=True, type=Path)
        command.add_argument("--task", required=True)

    resolve = subparsers.add_parser(
        "resolve",
        help="resolve one task to a live ready Template without creating it",
    )
    add_task_args(resolve)

    bind = subparsers.add_parser(
        "bind",
        help="bind one existing ready Template ID to a mapped task",
    )
    add_task_args(bind)
    bind.add_argument("--template-id", required=True)

    materialize = subparsers.add_parser(
        "materialize",
        help="explicitly create or reuse one mapped Template and record its ID",
    )
    add_task_args(materialize)
    materialize.add_argument(
        "--timeout",
        type=manager._positive_timeout,
        default=manager.DEFAULT_BUILD_TIMEOUT_SEC,
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = parse_args(argv)
    try:
        client = manager.client_from_environment()
        if args.command == "resolve":
            template_id = resolve_task_template(args.mapping, args.task, client)
        elif args.command == "bind":
            template_id = bind_task_template(
                args.mapping,
                args.task,
                args.template_id,
                client,
            )
        else:
            template_id = materialize_task_template(
                args.mapping,
                args.task,
                client,
                timeout=args.timeout,
                stderr=stderr,
            )
        print(template_id, file=stdout)
        return 0
    except (OSError, manager.QzTemplateError, QzTemplateResolutionError) as exc:
        print(f"error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
