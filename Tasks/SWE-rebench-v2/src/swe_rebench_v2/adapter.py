from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from swe_rebench_v2.environment import (
    render_environment_dockerfile,
    resolve_base_image,
)
from swe_rebench_v2.instance_spec import InstanceSpec, Record
from swe_rebench_v2.runtime import render_test_commands
from swe_rebench_v2.utils import read_text, render_literal, toml_escape


class HarborTaskPaths:
    def __init__(self, task_dir: Path) -> None:
        self.task_dir = task_dir
        self.environment_dir = task_dir / "environment"
        self.tests_dir = task_dir / "tests"
        self.solution_dir = task_dir / "solution"
        self.instruction_path = task_dir / "instruction.md"
        self.config_path = task_dir / "task.toml"
        self.test_sh_path = self.tests_dir / "test.sh"
        self.parser_path = self.tests_dir / "parser.py"
        self.config_json_path = self.tests_dir / "config.json"
        self.dockerfile_path = self.environment_dir / "Dockerfile"
        self.solve_sh_path = self.solution_dir / "solve.sh"

    def create(self) -> None:
        self.environment_dir.mkdir(parents=True)
        self.tests_dir.mkdir()
        self.solution_dir.mkdir()


@dataclass
class ConversionSummary:
    source: str
    total: int = 0
    converted: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    base_images: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "total": self.total,
            "converted": self.converted,
            "failed": len(self.failures),
            "failures": self.failures,
            "unique_base_images": sorted(self.base_images),
        }


class SWERebenchV2Adapter:
    def __init__(
        self,
        output_dir: Path,
        *,
        source: str,
        max_timeout_sec: float = 3000.0,
        template_dir: Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.max_timeout_sec = max_timeout_sec
        self.template_dir = template_dir or (Path(__file__).parent / "task-template")

    @staticmethod
    def _validate_local_task_id(local_task_id: str) -> None:
        if not local_task_id or local_task_id in {".", ".."}:
            raise ValueError(f"Invalid local task ID: {local_task_id!r}")
        if Path(local_task_id).name != local_task_id:
            raise ValueError(
                f"Local task ID must not contain a path: {local_task_id!r}"
            )

    def generate_task(
        self,
        record: Record,
        *,
        local_task_id: str | None = None,
        overwrite: bool = False,
    ) -> tuple[Path, str]:
        spec = InstanceSpec.from_record(record)
        local_task_id = local_task_id or spec.instance_id
        self._validate_local_task_id(local_task_id)
        task_dir = self.output_dir / local_task_id

        if task_dir.exists():
            if not overwrite:
                raise FileExistsError(f"Target already exists: {task_dir}")
            shutil.rmtree(task_dir)

        paths = HarborTaskPaths(task_dir)
        try:
            paths.create()

            instruction = dedent(spec.problem_statement).strip() + "\n"
            paths.instruction_path.write_text(instruction, encoding="utf-8")

            task_config = render_literal(
                read_text(self.template_dir / "task.toml"),
                instance_id=toml_escape(spec.instance_id),
                difficulty=toml_escape(spec.difficulty),
                max_timeout=str(int(self.max_timeout_sec)),
                source=toml_escape(f"{self.source}/{spec.instance_id}"),
            )
            paths.config_path.write_text(task_config, encoding="utf-8")

            paths.config_json_path.write_text(
                json.dumps(spec.raw, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )

            test_script = render_literal(
                read_text(self.template_dir / "tests" / "test.sh"),
                test_commands=render_test_commands(spec),
            )
            paths.test_sh_path.write_text(test_script, encoding="utf-8")
            paths.test_sh_path.chmod(0o755)
            shutil.copy2(self.template_dir / "tests" / "parser.py", paths.parser_path)
            shutil.copy2(
                self.template_dir / "tests" / "log_parsers.py",
                paths.tests_dir / "log_parsers.py",
            )
            shutil.copy2(
                self.template_dir / "tests" / "swe_constants.py",
                paths.tests_dir / "swe_constants.py",
            )

            dockerfile = render_environment_dockerfile(
                spec,
                self.template_dir / "environment" / "combine.Dockerfile.j2",
            )
            paths.dockerfile_path.write_text(dockerfile, encoding="utf-8")

            patch = (spec.patch or "").rstrip("\n")
            solve_script = render_literal(
                read_text(self.template_dir / "solution" / "solve.sh"),
                patch=patch,
                repo_dir=shlex.quote(spec.project_dir),
            )
            paths.solve_sh_path.write_text(solve_script, encoding="utf-8")
            paths.solve_sh_path.chmod(0o755)
        except Exception:
            if task_dir.exists():
                shutil.rmtree(task_dir)
            raise

        return task_dir, resolve_base_image(spec)

    def generate_many(
        self,
        records: Iterable[Record],
        *,
        overwrite: bool = False,
    ) -> ConversionSummary:
        summary = ConversionSummary(source=self.source)
        for record in records:
            summary.total += 1
            instance_id = str(record.get("instance_id") or f"row-{summary.total}")
            try:
                task_dir, base_image = self.generate_task(record, overwrite=overwrite)
            # A malformed row must not abort conversion of the remaining dataset.
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                print(f"[{summary.total}] FAIL {instance_id}: {reason}")
                summary.failures.append({"instance_id": instance_id, "reason": reason})
                continue
            print(f"[{summary.total}] OK   {instance_id} -> {task_dir}")
            summary.converted += 1
            summary.base_images.add(base_image)
        return summary
