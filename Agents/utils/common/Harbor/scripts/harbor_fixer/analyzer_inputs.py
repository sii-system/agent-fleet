"""Translate Analyzer output artifacts into validated Fixer task inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_io import read_json, read_jsonl
from .validation import (
    ValidationError,
    task_key,
    validate_analyzer_manifest,
    validate_env_infra_tasks,
    validate_fix_line_index,
    validate_task_input,
)


def _path_component(value: Any, name: str) -> str:
    text = str(value or "")
    if not text or Path(text).name != text or text in {".", ".."}:
        raise ValidationError(f"{name} must be a path-safe identifier")
    return text


def resolve_analyzer_paths(analyzer_output_path: Path) -> dict[str, Any]:
    """Resolve every handover's current publication from the Analyzer manifest."""

    analyzer_root = analyzer_output_path.expanduser().resolve()
    if not analyzer_root.is_dir():
        raise ValidationError("analyzer output must be the Analyzer root directory")
    manifest_path = analyzer_root / "analyzer-artifacts-latest.json"
    manifest = read_json(manifest_path)
    validate_analyzer_manifest(manifest)

    publications: list[dict[str, str]] = []
    for index, item in enumerate(manifest["publications"]):
        handover_id = _path_component(
            item["handover_id"],
            f"publications[{index}].handover_id",
        )
        publication_id = _path_component(
            item["publication_id"],
            f"publications[{index}].publication_id",
        )
        publications.append(
            {
                "handover_id": handover_id,
                "publication_id": publication_id,
                "env_infra_tasks_path": str(
                    analyzer_root
                    / "env-infra-tasks"
                    / handover_id
                    / f"{publication_id}.json"
                ),
                "fix_line_index_path": str(
                    analyzer_root
                    / "fix-line-index"
                    / handover_id
                    / f"{publication_id}.jsonl"
                ),
            }
        )
    return {
        "analyzer_root": analyzer_root,
        "manifest_path": manifest_path,
        "run_id": manifest["run_id"],
        "publications": publications,
    }


def _index_fix_lines(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        task = record["task"]
        indexed.setdefault(task_key(task), []).append(record)
    return indexed


def _identity_from_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_index": task["task_index"],
        "task_name": task["task_name"],
        "attempt_id": task["attempt_id"],
    }


def build_task_inputs(
    analyzer_output_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved = resolve_analyzer_paths(analyzer_output_path)
    source = {
        "analyzer_root": str(resolved["analyzer_root"]),
        "manifest_path": str(resolved["manifest_path"]),
        "run_id": resolved["run_id"],
        "publications": resolved["publications"],
    }

    task_inputs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for publication in resolved["publications"]:
        env_infra = read_json(Path(publication["env_infra_tasks_path"]))
        fix_lines = read_jsonl(Path(publication["fix_line_index_path"]))
        validate_env_infra_tasks(env_infra)
        validate_fix_line_index(fix_lines)
        if env_infra.get("handover_id") != publication["handover_id"]:
            raise ValidationError("env/infra handover_id does not match analyzer manifest")

        env_tasks = {task_key(item["task"]): item for item in env_infra["tasks"]}
        fix_line_index = _index_fix_lines(fix_lines)
        for key, records in fix_line_index.items():
            item = env_tasks.get(key)
            if item is None:
                raise ValidationError("fix-line-index task is absent from env/infra tasks")
            if any(record["root_cause_code"] != item["root_cause_code"] for record in records):
                raise ValidationError("fix-line-index root_cause_code does not match env/infra task")

        task_source = {
            "handover_id": publication["handover_id"],
            "publication_id": publication["publication_id"],
            "run_id": source["run_id"],
            "env_infra_tasks_path": publication["env_infra_tasks_path"],
            "fix_line_index_path": publication["fix_line_index_path"],
        }
        for item in env_infra["tasks"]:
            task = item["task"]
            evidence_records = fix_line_index.get(task_key(task), [])
            evidence = [
                {
                    "path": record["path"],
                    "line_start": record["line_start"],
                    "line_end": record["line_end"],
                    "fact": record["fact"],
                    "reason": record["reason"],
                }
                for record in evidence_records
            ]
            payload = {
                "schema_version": 1,
                "kind": "harbor_fixer_task_input",
                "source": task_source,
                "task": _identity_from_task(task),
                "analyzer_result": {
                    "final_class": item["final_class"],
                    "failure_stage": item["failure_stage"],
                    "scope": item["scope"],
                    "confidence": float(item["confidence"]),
                    "root_cause_code": item["root_cause_code"],
                    "root_cause_summary": item["root_cause_summary"],
                },
                "evidence": evidence,
            }
            validate_task_input(payload)
            # Later publications supersede older snapshots of the same benchmark task.
            task_inputs[task_key(task)] = payload
    return list(task_inputs.values()), source
