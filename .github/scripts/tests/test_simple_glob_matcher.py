from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
MATCHER_MODULE = (
    REPO_ROOT
    / "Agents"
    / "utils"
    / "common"
    / "Harbor"
    / "scripts"
    / "harbor_analyzer"
    / "pi_extensions"
    / "simple_glob_matcher.mjs"
)


class SimpleGlobMatcherTest(unittest.TestCase):
    def _run_node(
        self,
        script: str,
        *,
        timeout: float = 10,
    ) -> dict | list:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for pi extension tests")
        completed = subprocess.run(
            [
                node,
                "--input-type=module",
                "--eval",
                script,
                MATCHER_MODULE.as_uri(),
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr,
        )
        return json.loads(completed.stdout)

    def test_fixed_legacy_semantics(self) -> None:
        cases = [
            ["", "", True],
            ["", "a", False],
            ["*", "", True],
            ["*", "a/b", True],
            ["?", "/", True],
            ["?", "😀", False],
            ["??", "😀", True],
            ["a*b", "ab", True],
            ["a*b", "a/path/b", True],
            ["a?b", "a/b", True],
            ["a?b", "ab", False],
            ["**a***b", "a/path/b", True],
            ["[a].js", "[a].js", True],
            ["[a].js", "a.js", False],
            ["*", "\n", False],
            ["?", "\r", False],
            ["?", "\u2028", False],
            ["?", "\u2029", False],
            ["\n", "\n", True],
        ]
        script = f"""
const {{ compileSimpleGlob }} = await import(process.argv[1]);
const cases = {json.dumps(cases)};
const failures = cases
  .map(([pattern, value, expected]) => ({{
    pattern,
    value,
    expected,
    actual: compileSimpleGlob(pattern)(value),
  }}))
  .filter((item) => item.actual !== item.expected);
console.log(JSON.stringify(failures));
"""

        self.assertEqual(self._run_node(script), [])

    def test_seeded_equivalence_to_legacy_regex(self) -> None:
        script = r"""
const { compileSimpleGlob } = await import(process.argv[1]);
function legacyMatcher(pattern) {
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`);
}
let seed = 0x5eed1234;
function randomInt(limit) {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed % limit;
}
function randomString(tokens, maxLength) {
  const length = randomInt(maxLength + 1);
  let value = "";
  for (let index = 0; index < length; index++) {
    value += tokens[randomInt(tokens.length)];
  }
  return value;
}
const patternTokens = [
  "a", "b", "/", "\\", ".", "[", "]", "$", "😀", "*", "?",
];
const valueTokens = [
  "a", "b", "/", "\\", ".", "[", "]", "$", "😀", "\n", "\r",
  "\u2028", "\u2029",
];
const failures = [];
for (let index = 0; index < 5000; index++) {
  const pattern = randomString(patternTokens, 8);
  const value = randomString(valueTokens, 10);
  const expected = legacyMatcher(pattern).test(value);
  const actual = compileSimpleGlob(pattern)(value);
  if (actual !== expected) {
    failures.push({ pattern, value, expected, actual });
    if (failures.length === 10) break;
  }
}
console.log(JSON.stringify(failures));
"""

        self.assertEqual(self._run_node(script), [])

    def test_worst_case_5000_candidates_completes_promptly(self) -> None:
        script = r"""
import { performance } from "node:perf_hooks";
const { compileSimpleGlob } = await import(process.argv[1]);
const matcher = compileSimpleGlob(`${"*a".repeat(127)}b`);
const candidate = "a".repeat(4096);
const started = performance.now();
let matches = 0;
for (let index = 0; index < 5000; index++) {
  if (matcher(candidate)) matches++;
}
console.log(JSON.stringify({
  matches,
  elapsed_ms: performance.now() - started,
}));
"""

        result = self._run_node(script, timeout=10)
        self.assertEqual(result["matches"], 0)
        self.assertLess(result["elapsed_ms"], 10_000)


if __name__ == "__main__":
    unittest.main()
