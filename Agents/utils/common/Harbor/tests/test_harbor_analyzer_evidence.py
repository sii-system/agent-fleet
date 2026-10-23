"""Exercise the actual Analyzer evidence tools against oversized nightly-style logs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

EXTENSIONS = Path(__file__).resolve().parents[1] / "scripts/harbor_analyzer/pi_extensions"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Analyzer tools require Node")
class AnalyzerEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_node(self, script, *, extension=False):
        args = [NODE]
        if extension:
            version = subprocess.check_output([NODE, "--version"], text=True)
            if int(re.match(r"v(\d+)", version)[1]) < 22:
                self.skipTest("TypeScript integration requires Node 22+")
            args.append("--experimental-strip-types")
        result = subprocess.run(
            [*args, "--input-type=module", "-e", script],
            text=True, capture_output=True, check=False, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def tool_script(self, calls):
        return f"""
import register from {json.dumps((EXTENSIONS / 'analyzer_path_gate.ts').as_uri())};
import {{readFile}} from 'node:fs/promises';
const root = {json.dumps(str(self.root))};
process.env.HARBOR_ANALYZER_ALLOWED_PATHS_JSON = JSON.stringify([root]);
process.env.HARBOR_ANALYZER_ACCESS_AUDIT_PATH = root + '/audit.jsonl';
const tools = new Map();
register({{registerTool(tool) {{ tools.set(tool.name, tool); }}}});
const results = [];
for (const [tool, params] of {json.dumps(calls)}) {{
  results.push(await tools.get(tool).execute('test', params, null, null, {{cwd: root}}));
}}
const audit = (await readFile(root + '/audit.jsonl', 'utf8')).trim().split(String.fromCharCode(10)).map(JSON.parse);
console.log(JSON.stringify({{results, audit}}));
"""

    def test_long_jsonl_read_is_bounded_redacted_and_keeps_source_lines(self):
        path = self.root / "agent.jsonl"
        path.write_text('api_key=fake-private-key ' + '界' * 400_000 + ' FINAL_ERROR\nnext\n')
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name) / "private.txt"
        outside.write_text("outside evidence roots")
        data = self.run_node(self.tool_script([
            ["read", {"path": str(path), "limit": 1200}],
            ["read", {"path": str(outside)}],
        ]), extension=True)
        text = data["results"][0]["content"][0]["text"]
        self.assertLessEqual(len(text.encode()), 48 * 1024)
        self.assertNotIn("fake-private-key", text)
        self.assertIn("FINAL_ERROR", text)
        self.assertIn("L2: next", text)
        self.assertNotIn("\ufffd", text)
        read = next(x for x in data["audit"] if x.get("tool") == "read" and "line_end" in x)
        self.assertEqual((read["line_start"], read["line_end"]), (1, 3))
        self.assertTrue(read["truncated"])
        self.assertTrue(data["results"][1]["details"].get("blocked"))

    def test_grep_finds_middle_of_long_lines_with_one_total_byte_budget(self):
        path = self.root / "agent.jsonl"
        path.write_text(('x' * 40_000 + ' MODEL_NOT_FOUND ' + 'y' * 40_000 + '\n') * 100)
        data = self.run_node(self.tool_script([
            ["grep", {"path": str(path), "pattern": "MODEL_NOT_FOUND", "context": 1000, "limit": 10000}],
        ]), extension=True)
        text = data["results"][0]["content"][0]["text"]
        self.assertLessEqual(len(text.encode()), 48 * 1024)
        self.assertIn("MODEL_NOT_FOUND", text)
        self.assertTrue(data["results"][0]["details"]["truncated"])
        audit = next(x for x in data["audit"] if x.get("tool") == "grep" and "matches" in x)
        for match in audit["matches"]:
            self.assertIn(f"L{match['line_start']}:", text)
            self.assertIn(f"L{match['line_end']}:", text)

    def test_file_size_limit_rejects_oversized_files_and_binary_reads(self):
        large = self.root / "large.log"
        with large.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024 + 1)
        binary = self.root / "binary"
        binary.write_bytes(b"a\x00b")
        text = self.root / "text"
        text.write_text("normal evidence\n")
        data = self.run_node(f"""
import {{readTextFile}} from {json.dumps((EXTENSIONS / 'evidence_limits.mjs').as_uri())};
let error;
try {{ await readTextFile({json.dumps(str(large))}); }} catch (e) {{ error = e.message; }}
console.log(JSON.stringify({{
 error,
 binary: (await readTextFile({json.dumps(str(binary))})) === undefined,
 text: await readTextFile({json.dumps(str(text))})
}}));
""")
        self.assertIn("32 MiB", data["error"])
        self.assertTrue(data["binary"])
        self.assertEqual(data["text"], "normal evidence\n")

    def test_grep_reports_skipped_oversized_files_as_incomplete(self):
        large = self.root / "large.log"
        with large.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024 + 1)
        data = self.run_node(self.tool_script([
            ["grep", {"path": str(large), "pattern": "ERROR"}],
        ]), extension=True)
        result = data["results"][0]
        self.assertEqual(result["details"]["skipped_files"], 1)
        self.assertTrue(result["details"]["truncated"])
        self.assertIn("Incomplete search", result["content"][0]["text"])

    def test_directory_tools_report_entry_caps_and_complete_boundaries(self):
        directory = self.root / "inventory"
        directory.mkdir()
        for index in range(200):
            (directory / f"entry-{index:03}.log").touch()
        calls = [
            ["ls", {"path": str(directory), "limit": 10000}],
            ["find", {"path": str(directory), "pattern": "*", "limit": 10000}],
        ]
        complete = self.run_node(self.tool_script(calls), extension=True)
        for result, count_key in zip(complete["results"], ("entry_count", "match_count")):
            self.assertEqual(result["details"][count_key], 200)
            self.assertFalse(result["details"]["truncated"])

        (directory / "entry-200.log").touch()
        capped = self.run_node(self.tool_script(calls), extension=True)
        for result, count_key in zip(capped["results"], ("entry_count", "match_count")):
            self.assertEqual(result["details"][count_key], 200)
            self.assertTrue(result["details"]["truncated"])
            self.assertIn("truncated", result["content"][0]["text"])
            self.assertLessEqual(len(result["content"][0]["text"].encode()), 48 * 1024)

        for _, params in calls:
            params["limit"] = 2
        limited = self.run_node(self.tool_script(calls), extension=True)
        for result, count_key in zip(limited["results"], ("entry_count", "match_count")):
            self.assertEqual(result["details"][count_key], 2)
            self.assertTrue(result["details"]["truncated"])
            self.assertIn("truncated", result["content"][0]["text"])

    def test_searches_report_truncated_traversal_even_without_matches(self):
        directory = self.root / "inventory"
        directory.mkdir()
        for index in range(5000):
            (directory / f"entry-{index:04}.log").touch()
        (directory / "zz-last.log").write_text("hidden match\n")
        data = self.run_node(self.tool_script([
            ["find", {"path": str(directory), "pattern": "zz-last.log"}],
            ["grep", {"path": str(directory), "pattern": "hidden match"}],
        ]), extension=True)
        for result in data["results"]:
            self.assertEqual(result["details"]["match_count"], 0)
            self.assertTrue(result["details"]["truncated"])
            self.assertIn("truncated", result["content"][0]["text"])
