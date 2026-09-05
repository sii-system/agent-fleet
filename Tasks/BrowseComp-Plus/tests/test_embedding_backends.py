from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

BENCHMARK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK))
from retriever.config import RetrieverConfig  # noqa: E402
from retriever.embeddings import (  # noqa: E402
    OpenAIEmbeddingEncoder,
    normalize_openai_base_url,
)


class EmbeddingBackendTest(unittest.TestCase):
    def test_openai_config_keeps_credentials_out_of_the_server_command(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BROWSECOMP_INDEX_PATH": "/fixture/corpus.shard*.pkl",
                "BROWSECOMP_EMBEDDING_BACKEND": "openai",
                "BROWSECOMP_EMBEDDING_BASE_URL": "https://embed.example/api",
                "BROWSECOMP_EMBEDDING_API_KEY_ENV": "EMBEDDING_API_KEY",
                "BROWSECOMP_EMBEDDING_API_MODEL": "qwen-embedding-deployment",
                "EMBEDDING_API_KEY": "secret-value",
            },
            clear=True,
        ):
            config = RetrieverConfig.from_env(Path("/fixture/source"))
            command = config.command()

        self.assertIn("--embedding-backend", command)
        self.assertIn("openai", command)
        self.assertIn("--embedding-base-url", command)
        self.assertIn("https://embed.example/api", command)
        self.assertIn("EMBEDDING_API_KEY", command)
        self.assertNotIn("secret-value", command)
        self.assertIn("Qwen/Qwen3-Embedding-0.6B", command)

    def test_openai_backend_requires_a_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BROWSECOMP_INDEX_PATH": "/fixture/corpus.shard*.pkl",
                "BROWSECOMP_EMBEDDING_BACKEND": "openai",
            },
            clear=True,
        ), self.assertRaisesRegex(ValueError, "BASE_URL is required"):
            RetrieverConfig.from_env(Path("/fixture/source"))

    def test_openai_encoder_uses_standard_embeddings_api(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeEmbeddings:
            def create(self, **kwargs: object) -> object:
                calls.append(kwargs)
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[1.0, 2.0, 3.0])]
                )

        class FakeOpenAI:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs
                self.embeddings = FakeEmbeddings()

        fake_module = types.SimpleNamespace(OpenAI=FakeOpenAI)
        with (
            patch.dict(os.environ, {"EMBEDDING_API_KEY": "fixture-key"}, clear=True),
            patch.dict(sys.modules, {"openai": fake_module}),
        ):
            encoder = OpenAIEmbeddingEncoder(
                "https://embed.example/v1/embeddings",
                "EMBEDDING_API_KEY",
                "qwen-embedding-deployment",
                12.5,
                3,
            )
            vector = encoder.encode("prefixed query")

        self.assertEqual(vector.shape, (1, 3))
        self.assertEqual(vector.dtype, np.float32)
        self.assertEqual(
            calls,
            [
                {
                    "model": "qwen-embedding-deployment",
                    "input": ["prefixed query"],
                }
            ],
        )
        self.assertEqual(
            encoder.client.kwargs["base_url"], "https://embed.example/v1"
        )

    def test_openai_base_url_accepts_root_or_direct_endpoint(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://embed.example"),
            "https://embed.example/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("https://embed.example/v1/embeddings"),
            "https://embed.example/v1",
        )
        with self.assertRaisesRegex(ValueError, "absolute http"):
            normalize_openai_base_url("embed.example/v1")


if __name__ == "__main__":
    unittest.main()
