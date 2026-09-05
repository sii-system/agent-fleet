"""Select the upstream local judge or Agent Fleet's compatible API adapter."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def append_no_proxy_host(env: dict[str, str], url: str) -> None:
    host = urlparse(url).hostname or ""
    if not host:
        return
    existing = env.get("NO_PROXY", env.get("no_proxy", ""))
    values = [value.strip() for value in existing.split(",") if value.strip()]
    if host not in values:
        values.append(host)
    combined = ",".join(values)
    env["NO_PROXY"] = combined
    env["no_proxy"] = combined


@dataclass(frozen=True)
class JudgeConfig:
    mode: str
    source_root: Path
    ground_truth: Path
    eval_root: Path
    model: str
    qrel_evidence: Path

    @classmethod
    def from_env(cls, source_root: Path, ground_truth: Path, eval_root: Path) -> JudgeConfig:
        mode = os.environ.get("BROWSECOMP_JUDGE_MODE", "none")
        default_model = (
            os.environ.get("MODEL") if mode == "openai" else "Qwen/Qwen3-32B"
        )
        return cls(
            mode=mode,
            source_root=source_root.resolve(),
            ground_truth=ground_truth.resolve(),
            eval_root=eval_root.resolve(),
            model=(
                os.environ.get("BROWSECOMP_JUDGE_MODEL")
                or default_model
                or "Qwen/Qwen3-32B"
            ),
            qrel_evidence=Path(os.environ.get("BROWSECOMP_QREL_EVIDENCE", source_root / "topics-qrels" / "qrel_evidence.txt")).resolve(),
        )

    def command(self, input_dir: Path, force: bool = False) -> list[str]:
        if self.mode not in {"local", "openai"}:
            raise ValueError("judge command requires BROWSECOMP_JUDGE_MODE=local or openai")
        python = os.environ.get("BROWSECOMP_JUDGE_PYTHON", os.environ.get("BROWSECOMP_PYTHON", sys.executable))
        if self.mode == "local":
            script = self.source_root / "scripts_evaluation" / "evaluate_run.py"
        else:
            script = Path(__file__).resolve().with_name("evaluate_openai.py")
        command = [
            python,
            str(script),
            "--input_dir",
            str(input_dir.resolve()),
            "--ground_truth",
            str(self.ground_truth),
            "--eval_dir",
            str(self.eval_root),
            "--qrel_evidence",
            str(self.qrel_evidence),
            "--model",
            self.model,
        ]
        if force:
            command.append("--force")
        if self.mode == "local":
            command.extend(["--tensor_parallel_size", os.environ.get("BROWSECOMP_JUDGE_TENSOR_PARALLEL_SIZE", "1")])
        else:
            command.extend(["--source-root", str(self.source_root)])
            base_url = os.environ.get("BROWSECOMP_JUDGE_BASE_URL", "")
            if not base_url and os.environ.get("BASE_URL"):
                base_url = os.environ["BASE_URL"].rstrip("/")
                for suffix in ("/chat/completions", "/responses"):
                    if base_url.endswith(suffix):
                        base_url = base_url[: -len(suffix)]
                        break
                if not base_url.endswith("/v1"):
                    base_url += "/v1"
            if base_url:
                command.extend(["--base_url", base_url])
            command.extend(["--api_key_env", os.environ.get("BROWSECOMP_JUDGE_API_KEY_ENV", "API_KEY")])
            command.extend(["--api_mode", os.environ.get("BROWSECOMP_JUDGE_API_MODE", "chat-completions")])
        return command

    def evaluate(self, input_dir: Path, force: bool = False) -> None:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proxy_mode = env.get("BROWSECOMP_JUDGE_PROXY_MODE", "direct").strip().lower()
        if proxy_mode not in {"direct", "inherit"}:
            raise ValueError(
                "BROWSECOMP_JUDGE_PROXY_MODE must be direct or inherit"
            )
        if self.mode == "openai" and proxy_mode == "direct":
            append_no_proxy_host(
                env,
                env.get("BROWSECOMP_JUDGE_BASE_URL", env.get("BASE_URL", "")),
            )
        subprocess.run(
            self.command(input_dir, force),
            cwd=self.source_root,
            env=env,
            check=True,
        )
