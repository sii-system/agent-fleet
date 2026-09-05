"""Query embedding backends for the fixed BrowseComp FAISS index."""

from __future__ import annotations

import os
import threading
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import numpy as np

QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages "
    "that answer the query\nQuery:"
)


class QueryEmbedder(Protocol):
    """Encode exactly one already-prefixed query into a rank-2 numpy array."""

    def encode(self, text: str) -> np.ndarray: ...


def normalize_openai_base_url(value: str) -> str:
    """Accept an OpenAI API root or a direct embeddings endpoint.

    The OpenAI Python client expects an API root ending in ``/v1``.  Accepting
    both forms avoids a common configuration error while keeping the emitted
    request on the standard ``/v1/embeddings`` route.
    """

    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "BROWSECOMP_EMBEDDING_BASE_URL must be an absolute http(s) URL"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            "BROWSECOMP_EMBEDDING_BASE_URL must not include query parameters or fragments"
        )
    path = parsed.path.rstrip("/")
    path = path.removesuffix("/embeddings")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OpenAIEmbeddingEncoder:
    """Use an OpenAI-compatible ``/v1/embeddings`` endpoint on the host."""

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"embedding API key is missing; set the environment variable {api_key_env}"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - installation is bootstrapped
            raise RuntimeError("the OpenAI client is required for remote embedding") from exc
        self.client = OpenAI(
            api_key=api_key,
            base_url=normalize_openai_base_url(base_url),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self.model = model

    def encode(self, text: str) -> np.ndarray:
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=[text],
            )
        except Exception as exc:
            raise RuntimeError(
                f"embedding API request failed for model {self.model!r}"
            ) from exc
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != 1:
            raise RuntimeError("embedding API must return exactly one embedding")
        vector = np.asarray(getattr(data[0], "embedding", None), dtype=np.float32)
        if vector.ndim != 1 or vector.size == 0:
            raise RuntimeError("embedding API returned an empty or malformed vector")
        return vector.reshape(1, -1)


class LocalQwenEncoder:
    """Load the pinned Transformer model only for the local backend."""

    def __init__(
        self,
        model_name: str,
        model_revision: str | None,
        tokenizer: Any,
        torch_dtype: str,
        max_length: int,
    ) -> None:
        import torch
        from transformers import AutoModel

        self.torch = torch
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if torch_dtype == "auto":
            torch_dtype = "float16" if self.device == "cuda" else "float32"
        self.dtype_name = torch_dtype
        dtype = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }[torch_dtype]
        self.model = AutoModel.from_pretrained(
            model_name,
            revision=model_revision,
            cache_dir=os.environ.get("HF_HOME"),
            torch_dtype=dtype,
            trust_remote_code=True,
        ).to(self.device)
        self.model.eval()
        self._model_lock = threading.Lock()

    def encode(self, text: str) -> np.ndarray:
        batch = self.tokenizer(
            text,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        batch = {name: tensor.to(self.device) for name, tensor in batch.items()}
        autocast = self.device == "cuda" and self.dtype_name != "float32"
        with self._model_lock, self.torch.no_grad(), self.torch.autocast(
            device_type=self.device, enabled=autocast
        ):
            hidden = self.model(**batch).last_hidden_state
            mask = batch["attention_mask"]
            if bool((mask[:, -1] == 1).all()):
                embedding = hidden[:, -1]
            else:
                lengths = mask.sum(dim=1) - 1
                rows = self.torch.arange(hidden.shape[0], device=hidden.device)
                embedding = hidden[rows, lengths]
        return embedding.float().cpu().numpy()
