"""FAISS retriever over the fixed BrowseComp corpus and published shards."""

from __future__ import annotations

import glob
import os
import pickle
from pathlib import Path

import faiss
import numpy as np
from datasets import load_dataset

from retriever.embeddings import (
    QUERY_PREFIX,
    LocalQwenEncoder,
    OpenAIEmbeddingEncoder,
)


class QwenFaissRetriever:
    def __init__(
        self,
        index_path: str,
        model_name: str,
        model_revision: str | None,
        dataset_name: str,
        dataset_revision: str | None,
        normalize: bool,
        torch_dtype: str,
        max_length: int,
        embedding_backend: str,
        embedding_base_url: str | None,
        embedding_api_key_env: str,
        embedding_api_model: str,
        embedding_api_timeout_seconds: float,
        embedding_api_max_retries: int,
        tokenizer_model: str,
        tokenizer_revision: str | None,
    ) -> None:
        self.index, self.lookup = self._load_index(index_path)
        self.normalize = normalize
        self.task_prefix = QUERY_PREFIX
        from transformers import AutoTokenizer

        # A tokenizer remains local in API mode solely to apply the existing
        # token-based snippet limit.  No embedding weights are loaded there.
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_model,
            revision=tokenizer_revision,
            cache_dir=os.environ.get("HF_HOME"),
            padding_side="left",
        )
        if embedding_backend == "local":
            self.embedder = LocalQwenEncoder(
                model_name,
                model_revision,
                self.tokenizer,
                torch_dtype,
                max_length,
            )
        elif embedding_backend == "openai":
            if not embedding_base_url:
                raise ValueError("embedding_base_url is required for the openai backend")
            self.embedder = OpenAIEmbeddingEncoder(
                embedding_base_url,
                embedding_api_key_env,
                embedding_api_model,
                embedding_api_timeout_seconds,
                embedding_api_max_retries,
            )
        else:
            raise ValueError(f"unsupported embedding backend: {embedding_backend}")
        dataset = load_dataset(
            dataset_name,
            revision=dataset_revision,
            split="train",
            cache_dir=os.environ.get("HF_DATASETS_CACHE"),
        )
        # Keep document bodies in the memory-mapped Arrow dataset. Materializing
        # a second Python dict of every 1.7 GB text duplicates most of the corpus
        # in RAM; only the compact docid -> row lookup is needed for random access.
        self.corpus = dataset
        self.document_rows = {
            str(docid): row for row, docid in enumerate(dataset["docid"])
        }
        if len(self.document_rows) != len(dataset):
            raise ValueError("BrowseComp corpus contains duplicate docids")

    @staticmethod
    def _load_index(pattern: str) -> tuple[faiss.Index, list[str]]:
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(f"retrieval index matched no files: {pattern}")
        index: faiss.Index | None = None
        lookup: list[str] = []
        for filename in files:
            with Path(filename).open("rb") as handle:
                representations, shard_lookup = pickle.load(handle)
            matrix = np.asarray(representations, dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[1] == 0:
                raise ValueError(f"invalid embedding shard: {filename}")
            if index is None:
                index = faiss.IndexFlatIP(matrix.shape[1])
            elif matrix.shape[1] != index.d:
                raise ValueError(f"embedding dimension mismatch: {filename}")
            index.add(matrix)
            lookup.extend(str(value) for value in shard_lookup)
        assert index is not None
        if faiss.get_num_gpus() > 0:
            index = faiss.index_cpu_to_all_gpus(index)
        return index, lookup

    def _encode(self, query: str) -> np.ndarray:
        embedding = np.asarray(
            self.embedder.encode(self.task_prefix + query), dtype=np.float32
        )
        if embedding.ndim != 2 or embedding.shape[0] != 1:
            raise ValueError("query embedder must return exactly one rank-2 vector")
        if embedding.shape[1] != self.index.d:
            raise ValueError(
                "embedding dimension mismatch: "
                f"API/model returned {embedding.shape[1]}, index expects {self.index.d}"
            )
        if not np.isfinite(embedding).all():
            raise ValueError("query embedder returned non-finite values")
        if self.normalize:
            norm = float(np.linalg.norm(embedding[0]))
            if norm == 0:
                raise ValueError("query embedder returned a zero vector")
            embedding /= norm
        return np.ascontiguousarray(embedding)

    def search(self, query: str, k: int) -> list[dict[str, object]]:
        scores, indices = self.index.search(self._encode(query), k)
        results: list[dict[str, object]] = []
        for score, index in zip(scores[0], indices[0]):
            if index < 0:
                continue
            docid = self.lookup[index]
            document = self.get_document(docid)
            if document is None:
                raise KeyError(f"index docid is absent from pinned corpus: {docid}")
            results.append(
                {"docid": docid, "score": float(score), "text": document["text"]}
            )
        return results

    def get_document(self, docid: str) -> dict[str, str] | None:
        normalized = str(docid)
        row = self.document_rows.get(normalized)
        if row is None:
            return None
        return {"docid": normalized, "text": str(self.corpus[row]["text"])}
