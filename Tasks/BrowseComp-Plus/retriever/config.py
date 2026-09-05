"""Build the Agent Fleet-owned BrowseComp MCP adapter command."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from retriever.truncation import DOCUMENT_MAX_TOKENS


@dataclass(frozen=True)
class RetrieverConfig:
    source_root: Path
    python: str
    searcher_type: str
    index_path: str
    embedding_model: str
    embedding_revision: str | None
    embedding_backend: str
    embedding_base_url: str | None
    embedding_api_key_env: str
    embedding_api_model: str
    embedding_api_timeout_seconds: float
    embedding_api_max_retries: int
    tokenizer_model: str
    tokenizer_revision: str | None
    host: str
    port: int
    k: int
    snippet_max_tokens: int
    dataset_name: str
    dataset_revision: str | None
    torch_dtype: str

    @classmethod
    def from_env(cls, source_root: Path) -> RetrieverConfig:
        default_model = "Qwen/Qwen3-Embedding-0.6B"
        default_model_revision = "c54f2e6e80b2d7b7de06f51cec4959f6b3e03418"
        default_dataset = "Tevatron/browsecomp-plus-corpus"
        model = os.environ.get("BROWSECOMP_EMBEDDING_MODEL", default_model)
        dataset = os.environ.get("BROWSECOMP_CORPUS_DATASET", default_dataset)
        embedding_backend = os.environ.get(
            "BROWSECOMP_EMBEDDING_BACKEND", "local"
        ).strip().lower()
        if embedding_backend not in {"local", "openai"}:
            raise ValueError(
                "BROWSECOMP_EMBEDDING_BACKEND must be local or openai"
            )
        embedding_base_url = os.environ.get(
            "BROWSECOMP_EMBEDDING_BASE_URL", ""
        ).strip()
        if embedding_backend == "openai" and not embedding_base_url:
            raise ValueError(
                "BROWSECOMP_EMBEDDING_BASE_URL is required when "
                "BROWSECOMP_EMBEDDING_BACKEND=openai"
            )
        api_key_env = os.environ.get(
            "BROWSECOMP_EMBEDDING_API_KEY_ENV", "API_KEY"
        ).strip()
        if not api_key_env:
            raise ValueError("BROWSECOMP_EMBEDDING_API_KEY_ENV must not be empty")
        timeout_seconds = float(
            os.environ.get("BROWSECOMP_EMBEDDING_API_TIMEOUT_SECONDS", "60")
        )
        if timeout_seconds <= 0:
            raise ValueError("BROWSECOMP_EMBEDDING_API_TIMEOUT_SECONDS must be positive")
        max_retries = int(
            os.environ.get("BROWSECOMP_EMBEDDING_API_MAX_RETRIES", "2")
        )
        if max_retries < 0:
            raise ValueError("BROWSECOMP_EMBEDDING_API_MAX_RETRIES must be non-negative")
        embedding_revision = os.environ.get("BROWSECOMP_EMBEDDING_REVISION") or (
            default_model_revision if model == default_model else None
        )
        tokenizer_model = os.environ.get("BROWSECOMP_TOKENIZER_MODEL") or (
            default_model if embedding_backend == "openai" else model
        )
        tokenizer_revision = os.environ.get("BROWSECOMP_TOKENIZER_REVISION") or (
            default_model_revision
            if tokenizer_model == default_model
            else (embedding_revision if tokenizer_model == model else None)
        )
        return cls(
            source_root=source_root.resolve(),
            python=os.environ.get("BROWSECOMP_PYTHON", sys.executable),
            searcher_type=os.environ.get("BROWSECOMP_SEARCHER_TYPE", "faiss"),
            index_path=os.environ.get("BROWSECOMP_INDEX_PATH", ""),
            embedding_model=model,
            embedding_revision=embedding_revision,
            embedding_backend=embedding_backend,
            embedding_base_url=embedding_base_url or None,
            embedding_api_key_env=api_key_env,
            embedding_api_model=os.environ.get(
                "BROWSECOMP_EMBEDDING_API_MODEL", model
            ),
            embedding_api_timeout_seconds=timeout_seconds,
            embedding_api_max_retries=max_retries,
            tokenizer_model=tokenizer_model,
            tokenizer_revision=tokenizer_revision,
            host=os.environ.get("BROWSECOMP_MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("BROWSECOMP_MCP_PORT", "8000")),
            k=int(os.environ.get("BROWSECOMP_SEARCH_K", "5")),
            snippet_max_tokens=int(os.environ.get("BROWSECOMP_SNIPPET_MAX_TOKENS", "512")),
            dataset_name=dataset,
            dataset_revision=os.environ.get("BROWSECOMP_CORPUS_REVISION")
            or (
                "1b854ae04817320c2a088c0ff9830ffcb92ca079"
                if dataset == default_dataset
                else None
            ),
            torch_dtype=os.environ.get("BROWSECOMP_TORCH_DTYPE", "auto"),
        )

    @property
    def local_url(self) -> str:
        probe_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{probe_host}:{self.port}/mcp"

    @property
    def public_url(self) -> str:
        return os.environ.get(
            "BROWSECOMP_MCP_PUBLIC_URL",
            f"http://host.docker.internal:{self.port}/mcp",
        )

    def command(self) -> list[str]:
        if self.searcher_type != "faiss":
            raise ValueError("Agent Fleet's built-in BrowseComp retriever currently supports faiss")
        if not self.index_path:
            raise ValueError("BROWSECOMP_INDEX_PATH is required")
        benchmark_dir = Path(__file__).resolve().parents[1]
        command = [
            self.python,
            str(benchmark_dir / "mcp" / "server.py"),
            "--index-path",
            self.index_path,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--k",
            str(self.k),
            "--snippet-max-tokens",
            str(self.snippet_max_tokens),
            "--document-max-tokens",
            str(DOCUMENT_MAX_TOKENS),
            "--dataset-name",
            self.dataset_name,
            "--torch-dtype",
            self.torch_dtype,
        ]
        command.extend(["--model-name", self.embedding_model])
        if self.embedding_revision:
            command.extend(["--model-revision", self.embedding_revision])
        command.extend(
            [
                "--embedding-backend",
                self.embedding_backend,
                "--embedding-api-key-env",
                self.embedding_api_key_env,
                "--embedding-api-model",
                self.embedding_api_model,
                "--embedding-api-timeout-seconds",
                str(self.embedding_api_timeout_seconds),
                "--embedding-api-max-retries",
                str(self.embedding_api_max_retries),
                "--tokenizer-model",
                self.tokenizer_model,
            ]
        )
        if self.embedding_base_url:
            command.extend(["--embedding-base-url", self.embedding_base_url])
        if self.tokenizer_revision:
            command.extend(["--tokenizer-revision", self.tokenizer_revision])
        if self.dataset_revision:
            command.extend(["--dataset-revision", self.dataset_revision])
        if os.environ.get("BROWSECOMP_NORMALIZE", "1").lower() in {"0", "false", "no"}:
            command.append("--no-normalize")
        return command
