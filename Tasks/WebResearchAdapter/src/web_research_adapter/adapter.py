from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import toml
import tomllib

PACKAGE_DIR = Path(__file__).parent
TEMPLATE_DIR = PACKAGE_DIR / "task-template"
EXPECTED_COUNTS = {"browsecomp": 1266, "deepsearchqa": 900}
EXPECTED_SHA256 = {
    "browsecomp": "7b24471cd5b3eb2a46830a14802b5c029ea62f488ff75a0f88af7923d1454abf",
    "deepsearchqa": "25d48dcf7efa872e5467032e8b8eedf38d301f59a252d0da95cda584baa78396",
}
AUTHORS = {"browsecomp": "OpenAI", "deepsearchqa": "Google"}
IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*")


@dataclass(frozen=True, slots=True)
class Example:
    id: str
    question: str
    answer: str
    topic: str = ""
    answer_type: str | None = None


def _decrypt(value: str, password: str) -> str:
    encrypted = base64.b64decode(value, validate=True)
    digest = hashlib.sha256(password.encode()).digest()
    key = (digest * (len(encrypted) // len(digest) + 1))[: len(encrypted)]
    return bytes(a ^ b for a, b in zip(encrypted, key)).decode()


def _rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
        return list(reader)


def load_browsecomp(path: Path) -> list[Example]:
    rows = _rows(path, {"problem", "answer", "canary"})
    return [
        Example(
            str(i),
            _decrypt(row["problem"], row["canary"]),
            _decrypt(row["answer"], row["canary"]),
            row.get("problem_topic", "").strip(),
        )
        for i, row in enumerate(rows)
    ]


def load_deepsearchqa(path: Path) -> list[Example]:
    rows = _rows(path, {"problem", "problem_category", "answer", "answer_type"})
    examples = []
    for i, row in enumerate(rows):
        answer_type = row["answer_type"].strip()
        if answer_type not in {"Single Answer", "Set Answer"}:
            raise ValueError(f"invalid answer_type at row {i}: {answer_type!r}")
        examples.append(
            Example(
                str(i),
                row["problem"].strip(),
                row["answer"].strip(),
                row["problem_category"].strip(),
                answer_type,
            )
        )
    return examples


class WebResearchAdapter:
    def __init__(
        self,
        benchmark: str,
        input_path: Path,
        output_dir: Path,
        *,
        image: str = "python:3.12-slim",
        overwrite: bool = False,
    ) -> None:
        if benchmark not in EXPECTED_COUNTS:
            raise ValueError(f"unsupported benchmark: {benchmark}")
        if not IMAGE_RE.fullmatch(image):
            raise ValueError(f"invalid image reference: {image!r}")
        self.benchmark = benchmark
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir)
        self.image = image
        self.overwrite = overwrite

    def _load(self) -> list[Example]:
        loader = (
            load_browsecomp if self.benchmark == "browsecomp" else load_deepsearchqa
        )
        return loader(self.input_path)

    def _task_id(self, source_id: str) -> str:
        return f"{self.benchmark}-{int(source_id):06d}"

    def _config(self, example: Example, task_id: str) -> str:
        config = tomllib.loads((TEMPLATE_DIR / "task.toml").read_text())
        config["task"].update(
            name=f"agent-fleet/{task_id}",
            description=f"{self.benchmark} web research task",
            authors=[{"name": AUTHORS[self.benchmark]}],
            keywords=[self.benchmark, "web-research", "rl"],
        )
        config["metadata"].update(
            benchmark=self.benchmark,
            source_id=example.id,
            topic=example.topic,
        )
        config["environment"]["docker_image"] = self.image
        return toml.dumps(config)

    def _write_task(self, example: Example) -> str:
        task_id = self._task_id(example.id)
        target = self.output_dir / task_id
        staged = self.output_dir / f".{task_id}.tmp-{os.getpid()}"
        backup = self.output_dir / f".{task_id}.bak-{os.getpid()}"
        if target.exists() and not self.overwrite:
            raise FileExistsError(target)
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(TEMPLATE_DIR, staged)
        (staged / "instruction.md").write_text(
            (staged / "instruction.md").read_text().rstrip()
            + f"\n\nQuestion:\n{example.question}\n"
        )
        (staged / "task.toml").write_text(self._config(example, task_id))
        dockerfile = (
            (staged / "environment/Dockerfile")
            .read_text()
            .replace("{image}", self.image)
        )
        (staged / "environment/Dockerfile").write_text(dockerfile)
        reference = {"benchmark": self.benchmark, **asdict(example)}
        if reference["answer_type"] is None:
            reference.pop("answer_type")
        (staged / "tests/reference.json").write_text(
            json.dumps(reference, ensure_ascii=False, indent=2) + "\n"
        )
        shutil.copy2(PACKAGE_DIR / "grader.py", staged / "tests/grader.py")
        (staged / "solution/answer.txt").write_text(example.answer + "\n")
        tomllib.loads((staged / "task.toml").read_text())
        for script in (staged / "tests/test.sh", staged / "solution/solve.sh"):
            script.chmod(0o755)
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
        except Exception:
            if backup.exists():
                backup.rename(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return task_id

    def run(
        self, task_ids: list[str] | None = None, limit: int | None = None
    ) -> list[str]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        examples = self._load()
        source_count = len(examples)
        source_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        expected_count = EXPECTED_COUNTS[self.benchmark]
        expected_sha256 = EXPECTED_SHA256[self.benchmark]
        if source_count != expected_count or source_sha256 != expected_sha256:
            raise ValueError(
                f"unexpected {self.benchmark} source: expected "
                f"count={expected_count} sha256={expected_sha256}, got "
                f"count={source_count} sha256={source_sha256}"
            )
        if task_ids:
            requested = {
                value.removeprefix(f"{self.benchmark}-").lstrip("0") or "0"
                for value in task_ids
            }
            known = {example.id for example in examples}
            if unknown := requested - known:
                raise ValueError(f"unknown task ids: {', '.join(sorted(unknown))}")
            examples = [example for example in examples if example.id in requested]
        examples = examples[:limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = [self._write_task(example) for example in examples]
        manifest = {
            "benchmark": self.benchmark,
            "expected_tasks": EXPECTED_COUNTS[self.benchmark],
            "source_tasks": source_count,
            "source_file": self.input_path.name,
            "source_sha256": source_sha256,
            "generated_tasks": len(generated),
            "task_ids": generated,
            "image": self.image,
        }
        (self.output_dir / "generation-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        return generated
