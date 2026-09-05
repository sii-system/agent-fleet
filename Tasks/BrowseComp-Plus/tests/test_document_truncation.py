from __future__ import annotations

import sys
import unittest
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK))
from retriever.truncation import (  # noqa: E402
    DOCUMENT_MAX_TOKENS,
    truncate_text,
)


class CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        self.assert_no_special_tokens = not add_special_tokens
        return [ord(character) for character in text]

    def decode(self, tokens: list[int], *, skip_special_tokens: bool) -> str:
        self.assert_skip_special_tokens = skip_special_tokens
        return "".join(chr(token) for token in tokens)


class DocumentTruncationTest(unittest.TestCase):
    def test_long_document_is_limited_to_the_first_4096_tokens(self) -> None:
        tokenizer = CharacterTokenizer()
        text = "a" * (DOCUMENT_MAX_TOKENS + 100)

        result = truncate_text(text, tokenizer, DOCUMENT_MAX_TOKENS)

        self.assertEqual(result, "a" * DOCUMENT_MAX_TOKENS)
        self.assertTrue(tokenizer.assert_no_special_tokens)
        self.assertTrue(tokenizer.assert_skip_special_tokens)

    def test_short_document_is_returned_unchanged(self) -> None:
        tokenizer = CharacterTokenizer()
        text = "short document\nwith original whitespace"

        self.assertIs(truncate_text(text, tokenizer, DOCUMENT_MAX_TOKENS), text)

    def test_non_positive_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            truncate_text("document", CharacterTokenizer(), 0)


if __name__ == "__main__":
    unittest.main()
