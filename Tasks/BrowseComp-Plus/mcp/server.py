#!/usr/bin/env python3
"""Agent Fleet-owned MCP wrapper around the fixed BrowseComp corpus."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fastmcp import FastMCP

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_DIR))
from retriever.faiss import QwenFaissRetriever  # noqa: E402
from retriever.truncation import (  # noqa: E402
    DOCUMENT_MAX_TOKENS,
    truncate_text,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-path", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-revision")
    parser.add_argument("--embedding-backend", choices=["local", "openai"], default="local")
    parser.add_argument("--embedding-base-url")
    parser.add_argument("--embedding-api-key-env", default="API_KEY")
    parser.add_argument("--embedding-api-model")
    parser.add_argument("--embedding-api-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--embedding-api-max-retries", type=int, default=2)
    parser.add_argument("--tokenizer-model")
    parser.add_argument("--tokenizer-revision")
    parser.add_argument("--dataset-name", default="Tevatron/browsecomp-plus-corpus")
    parser.add_argument("--dataset-revision")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--snippet-max-tokens", type=int, default=512)
    parser.add_argument(
        "--document-max-tokens", type=int, default=DOCUMENT_MAX_TOKENS
    )
    parser.add_argument("--max-length", type=int, default=8192)
    parser.add_argument(
        "--torch-dtype",
        choices=["auto", "float16", "bfloat16", "float32"],
        default="auto",
    )
    parser.add_argument("--no-normalize", action="store_true")
    args = parser.parse_args()
    if args.embedding_api_timeout_seconds <= 0:
        parser.error("--embedding-api-timeout-seconds must be positive")
    if args.embedding_api_max_retries < 0:
        parser.error("--embedding-api-max-retries must be non-negative")
    if args.document_max_tokens <= 0:
        parser.error("--document-max-tokens must be positive")

    retriever = QwenFaissRetriever(
        args.index_path,
        args.model_name,
        args.model_revision,
        args.dataset_name,
        args.dataset_revision,
        not args.no_normalize,
        args.torch_dtype,
        args.max_length,
        args.embedding_backend,
        args.embedding_base_url,
        args.embedding_api_key_env,
        args.embedding_api_model or args.model_name,
        args.embedding_api_timeout_seconds,
        args.embedding_api_max_retries,
        args.tokenizer_model or args.model_name,
        args.tokenizer_revision or args.model_revision,
    )
    mcp = FastMCP(name="browsecomp-search")

    @mcp.tool(
        name="search",
        description=f"Search the fixed BrowseComp corpus and return the top {args.k} documents.",
    )
    def search(query: str) -> list[dict[str, object]]:
        candidates = retriever.search(query, args.k)
        for candidate in candidates:
            text = str(candidate.pop("text"))
            if args.snippet_max_tokens > 0:
                tokens = retriever.tokenizer.encode(text, add_special_tokens=False)
                tokens = tokens[: args.snippet_max_tokens]
                text = retriever.tokenizer.decode(tokens, skip_special_tokens=True)
            candidate["snippet"] = text
        return candidates

    @mcp.tool(
        name="get_document",
        description=(
            "Retrieve the first "
            f"{args.document_max_tokens} tokens of a corpus document by docid."
        ),
    )
    def get_document(docid: str) -> dict[str, str] | None:
        document = retriever.get_document(docid)
        if document is None:
            return None
        document["text"] = truncate_text(
            document["text"], retriever.tokenizer, args.document_max_tokens
        )
        return document

    print(f"BrowseComp MCP listening at http://{args.host}:{args.port}/mcp", flush=True)
    mcp.run(
        transport="streamable-http",
        path="/mcp",
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
