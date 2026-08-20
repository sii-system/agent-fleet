"""Inventory Harbor task images and emit a deterministic QZ Template map."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    tomllib = None


SCHEMA_VERSION = 1
IDENTITY_VERSION = "qz-template-image-v1"
DEFAULT_SPEC = "g.c1"
DEFAULT_IMAGE_SOURCE = "official"
SPEC_CHOICES = ("g.c1", "g.c2", "g.c4")
TEMPLATE_NAME_MAX_LENGTH = 63
TEMPLATE_LABEL_MAX_LENGTH = 32
TEMPLATE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class QzTemplateMappingError(RuntimeError):
    """Raised when a benchmark cannot be represented by the mapping schema."""


def _fallback_environment_docker_image(path: Path) -> str:
    """Read one TOML string on Python versions that do not provide tomllib."""
    section = ""
    decoder = json.JSONDecoder()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != "environment" or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != "docker_image":
            continue
        value = raw_value.lstrip()
        if value.startswith('"'):
            try:
                parsed, end = decoder.raw_decode(value)
            except json.JSONDecodeError as exc:
                raise QzTemplateMappingError(
                    f"invalid environment.docker_image in {path}"
                ) from exc
            trailing = value[end:].strip()
            if trailing and not trailing.startswith("#"):
                raise QzTemplateMappingError(
                    f"invalid environment.docker_image in {path}"
                )
            return parsed
        if value.startswith("'"):
            end = value.find("'", 1)
            if end == -1:
                raise QzTemplateMappingError(
                    f"invalid environment.docker_image in {path}"
                )
            trailing = value[end + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                raise QzTemplateMappingError(
                    f"invalid environment.docker_image in {path}"
                )
            return value[1:end]
        raise QzTemplateMappingError(
            f"environment.docker_image must be a TOML string in {path}"
        )
    raise QzTemplateMappingError(
        f"task is missing environment.docker_image: {path.parent}"
    )


def load_task_image(task_dir: Path) -> str:
    """Return the prebuilt image declared by a local Harbor task."""
    task_config = task_dir / "task.toml"
    if not task_config.is_file():
        raise QzTemplateMappingError(f"task.toml not found under {task_dir}")

    if tomllib is None:
        value = _fallback_environment_docker_image(task_config)
    else:
        try:
            with task_config.open("rb") as handle:
                payload = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise QzTemplateMappingError(f"invalid TOML: {task_config}") from exc
        environment = payload.get("environment")
        value = (
            environment.get("docker_image")
            if isinstance(environment, Mapping)
            else None
        )

    if not isinstance(value, str) or not value.strip():
        raise QzTemplateMappingError(
            f"task is missing environment.docker_image: {task_dir}"
        )
    return value.strip()


def _task_list_entries(path: Path) -> list[str]:
    if not path.is_file():
        raise QzTemplateMappingError(f"task list not found: {path}")
    entries = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            entries.append(value)
    if not entries:
        raise QzTemplateMappingError(f"task list is empty: {path}")
    return entries


def discover_tasks(
    dataset_root: Path,
    task_list: Path | None = None,
) -> list[tuple[str, Path]]:
    """Discover task directories and return portable relative task keys."""
    root = dataset_root.expanduser().resolve()
    if not root.is_dir():
        raise QzTemplateMappingError(f"dataset root not found: {dataset_root}")

    if (root / "task.toml").is_file():
        if task_list is not None:
            raise QzTemplateMappingError(
                "--task-list cannot be used when --dataset-root is one task"
            )
        return [(root.name, root)]

    if task_list is None:
        candidates = sorted(
            path for path in root.iterdir() if (path / "task.toml").is_file()
        )
    else:
        candidates = []
        for entry in _task_list_entries(task_list):
            relative = Path(entry)
            if relative.is_absolute():
                raise QzTemplateMappingError(
                    f"task list entries must be relative paths: {entry}"
                )
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise QzTemplateMappingError(
                    f"task escapes dataset root: {entry}"
                ) from exc
            if not (candidate / "task.toml").is_file():
                raise QzTemplateMappingError(
                    f"task.toml not found for task list entry: {entry}"
                )
            candidates.append(candidate)

    if not candidates:
        raise QzTemplateMappingError(f"no Harbor tasks found under {root}")

    discovered: dict[str, Path] = {}
    for candidate in candidates:
        key = candidate.relative_to(root).as_posix()
        if key in discovered:
            raise QzTemplateMappingError(f"duplicate task in inventory: {key}")
        discovered[key] = candidate
    return sorted(discovered.items())


def template_identity(image: str, spec: str, image_source: str) -> str:
    """Return the stable identity for one QZ Template input tuple."""
    payload = json.dumps(
        {
            "identity_version": IDENTITY_VERSION,
            "image": image,
            "image_source": image_source,
            "spec": spec,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def template_name(image: str, identity: str) -> str:
    """Build a deterministic QZ-safe alias from an image and its identity."""
    leaf = image.rsplit("/", 1)[-1].split("@", 1)[0]
    label = re.sub(r"[^A-Za-z0-9]+", "_", leaf).strip("_").lower()
    label = (label or "image")[:TEMPLATE_LABEL_MAX_LENGTH].rstrip("_")
    name = f"af_{label}_{identity[:16]}"
    name = name[:TEMPLATE_NAME_MAX_LENGTH].rstrip("_")
    if TEMPLATE_NAME_PATTERN.fullmatch(name) is None:
        raise QzTemplateMappingError(f"failed to build a QZ-safe name for {image!r}")
    return name


def build_inventory(
    *,
    benchmark: str,
    tasks: Iterable[tuple[str, Path]],
    spec: str = DEFAULT_SPEC,
    image_source: str = DEFAULT_IMAGE_SOURCE,
) -> dict[str, Any]:
    """Build a deterministic schema-v1 task-to-Template inventory."""
    benchmark = benchmark.strip()
    image_source = image_source.strip()
    if not benchmark:
        raise QzTemplateMappingError("benchmark name must not be empty")
    if spec not in SPEC_CHOICES:
        raise QzTemplateMappingError(f"unsupported QZ spec: {spec}")
    if not image_source:
        raise QzTemplateMappingError("image source must not be empty")

    templates: dict[str, dict[str, Any]] = {}
    task_map: dict[str, dict[str, str]] = {}
    failures = []
    for task_key, task_dir in sorted(tasks):
        try:
            image = load_task_image(task_dir)
        except QzTemplateMappingError as exc:
            failures.append(f"{task_key}: {exc}")
            continue
        identity = template_identity(image, spec, image_source)
        template_key = f"sha256:{identity}"
        templates.setdefault(
            template_key,
            {
                "image": image,
                "image_source": image_source,
                "spec": spec,
                "template_id": None,
                "template_name": template_name(image, identity),
            },
        )
        task_map[task_key] = {
            "docker_image": image,
            "template_key": template_key,
        }

    if failures:
        details = "\n".join(f"- {failure}" for failure in failures)
        raise QzTemplateMappingError(
            f"cannot inventory {len(failures)} task(s):\n{details}"
        )

    return {
        "benchmark": benchmark,
        "identity_version": IDENTITY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "tasks": dict(sorted(task_map.items())),
        "templates": dict(sorted(templates.items())),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def preserve_template_ids(
    inventory: dict[str, Any],
    existing_path: Path,
) -> int:
    """Carry forward bindings whose full content identity is unchanged."""
    if not existing_path.is_file():
        return 0
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QzTemplateMappingError(
            f"existing mapping is not valid UTF-8 JSON: {existing_path}"
        ) from exc
    if not isinstance(existing, dict):
        raise QzTemplateMappingError(
            f"existing mapping must be a JSON object: {existing_path}"
        )
    if existing.get("schema_version") != SCHEMA_VERSION:
        raise QzTemplateMappingError(
            f"existing mapping has unsupported schema_version: {existing_path}"
        )
    if existing.get("identity_version") != IDENTITY_VERSION:
        raise QzTemplateMappingError(
            f"existing mapping has unsupported identity_version: {existing_path}"
        )
    existing_templates = existing.get("templates")
    if not isinstance(existing_templates, dict):
        raise QzTemplateMappingError(
            f"existing mapping is missing templates: {existing_path}"
        )

    preserved = 0
    for template_key, template in inventory["templates"].items():
        previous = existing_templates.get(template_key)
        if not isinstance(previous, dict):
            continue
        template_id = previous.get("template_id")
        if template_id is None:
            continue
        if not isinstance(template_id, str) or not template_id.strip():
            raise QzTemplateMappingError(
                f"existing mapping Template {template_key!r} has an invalid template_id"
            )
        template["template_id"] = template_id.strip()
        preserved += 1
    return preserved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory Harbor task images for QZ Template mapping."
    )
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--task-list", type=Path)
    parser.add_argument("--spec", choices=SPEC_CHOICES, default=DEFAULT_SPEC)
    parser.add_argument("--image-source", default=DEFAULT_IMAGE_SOURCE)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = parse_args(argv)
    try:
        tasks = discover_tasks(args.dataset_root, args.task_list)
        inventory = build_inventory(
            benchmark=args.benchmark,
            tasks=tasks,
            spec=args.spec,
            image_source=args.image_source,
        )
        if args.output is None:
            json.dump(inventory, stdout, ensure_ascii=False, indent=2, sort_keys=True)
            stdout.write("\n")
        else:
            preserve_template_ids(inventory, args.output.expanduser())
            _write_json(args.output, inventory)
            print(
                f"wrote {len(inventory['tasks'])} tasks and "
                f"{len(inventory['templates'])} unique images to {args.output}",
                file=stderr,
            )
        return 0
    except (OSError, QzTemplateMappingError, ValueError) as exc:
        print(f"error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
