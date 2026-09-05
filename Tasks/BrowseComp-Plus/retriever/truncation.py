"""Token-bound text returned by the BrowseComp retrieval tools."""

from __future__ import annotations

from typing import Protocol

DOCUMENT_MAX_TOKENS = 4096


class Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...

    def decode(self, tokens: list[int], *, skip_special_tokens: bool) -> str: ...


def truncate_text(text: str, tokenizer: Tokenizer, max_tokens: int) -> str:
    """Return at most ``max_tokens`` from the beginning of ``text``."""

    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    tokens = tokenizer.encode(text, add_special_tokens=False)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens], skip_special_tokens=True)
