from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SPEC = importlib.util.spec_from_file_location(
    "browsecomp_prepare_dataset", RUNTIME / "prepare_dataset.py"
)
assert SPEC is not None and SPEC.loader is not None
prepare_dataset = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_dataset)


class PrepareDatasetTest(unittest.TestCase):
    def test_loading_official_decryptor_keeps_upstream_tree_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)
            script_dir = source / "scripts_build_index"
            script_dir.mkdir()
            (script_dir / "decrypt_dataset.py").write_text(
                "DEFAULT_CANARY = 'fixture'\n"
                "def decrypt_string(value, canary):\n"
                "    return value + canary\n",
                encoding="utf-8",
            )

            module = prepare_dataset.load_official_decrypt(source)
            self.assertEqual(module.decrypt_string("x", module.DEFAULT_CANARY), "xfixture")
            self.assertFalse((script_dir / "__pycache__").exists())

    def test_projection_and_private_minimal_output(self) -> None:
        rows = [
            {
                "query_id": str(index),
                "query": f"encrypted-query-{index}",
                "answer": f"encrypted-answer-{index}",
                "gold_docs": [{"text": "must not be retained"}],
            }
            for index in range(prepare_dataset.EXPECTED_ROWS)
        ]

        def decrypt(value: str, canary: str) -> str:
            self.assertEqual(canary, "fixture-canary")
            return value.removeprefix("encrypted-")

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "private" / "gold.jsonl"
            queries = Path(root) / "private" / "queries.tsv"
            prepare_dataset.write_private_dataset(
                rows, output, queries, decrypt, "fixture-canary"
            )
            decoded = [json.loads(line) for line in output.read_text().splitlines()]

            self.assertEqual(len(decoded), prepare_dataset.EXPECTED_ROWS)
            self.assertEqual(
                decoded[0],
                {"query_id": "0", "query": "query-0", "answer": "answer-0"},
            )
            self.assertNotIn("gold_docs", decoded[0])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(queries.stat().st_mode), 0o600)

    def test_shards_are_parallelized_but_rows_keep_shard_order(self) -> None:
        rows = prepare_dataset.fetch_projected_rows(
            ["data/test-2.parquet", "data/test-1.parquet"],
            reader=lambda name: [{"query_id": name}],
        )
        self.assertEqual(
            [row["query_id"] for row in rows],
            ["data/test-1.parquet", "data/test-2.parquet"],
        )


if __name__ == "__main__":
    unittest.main()
