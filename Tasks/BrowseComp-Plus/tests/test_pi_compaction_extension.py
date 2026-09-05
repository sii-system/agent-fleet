from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1]
EXTENSION = (
    BENCHMARK / "integrations" / "pi" / "auto_continue_after_compaction.ts"
)


class PiCompactionExtensionTest(unittest.TestCase):
    def test_queues_only_incomplete_threshold_compactions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            module = root_path / "extension.mjs"
            module.write_text(EXTENSION.read_text(encoding="utf-8"), encoding="utf-8")
            harness = root_path / "harness.mjs"
            harness.write_text(
                """
import extension from './extension.mjs';
const handlers = {};
const calls = [];
const pi = {
  on(name, handler) { handlers[name] = handler; },
  sendUserMessage(content, options) { calls.push({ content, options }); },
};
extension(pi);
handlers.message_end({ message: { role: 'assistant', content: [{ type: 'text', text: 'Still searching' }] } });
handlers.session_compact({ reason: 'threshold', willRetry: false });
for (const completedAnswer of [
  'Exact Answer: Example\\nConfidence: 80%',
  '**Exact Answer**\\nCannot be determined from the retrieved corpus\\n**Confidence**\\nVery low (≈5%)',
  '**Exact Answer:** "Manos"\\n**Confidence:** 95%',
  '### Exact Answer\\n"Manos"\\n### Confidence\\n95%',
]) {
  handlers.message_end({ message: { role: 'assistant', content: [{ type: 'text', text: completedAnswer }] } });
  handlers.session_compact({ reason: 'threshold', willRetry: false });
}
handlers.message_end({ message: { role: 'assistant', content: [{ type: 'text', text: 'Interrupted again' }] } });
handlers.session_compact({ reason: 'overflow', willRetry: true });
console.log(JSON.stringify(calls));
""".strip()
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(harness)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = json.loads(completed.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["options"], {"deliverAs": "followUp"})
        self.assertIn("Continue from the retained summary", calls[0]["content"])

    def test_empty_markdown_answer_still_queues_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            module = root_path / "extension.mjs"
            module.write_text(EXTENSION.read_text(encoding="utf-8"), encoding="utf-8")
            harness = root_path / "harness.mjs"
            harness.write_text(
                """
import extension from './extension.mjs';
const handlers = {};
const calls = [];
const pi = {
  on(name, handler) { handlers[name] = handler; },
  sendUserMessage(content, options) { calls.push({ content, options }); },
};
extension(pi);
handlers.message_end({ message: { role: 'assistant', content: [{
  type: 'text',
  text: '**Exact Answer**\\n\\n**Confidence**\\n80%',
}] } });
handlers.session_compact({ reason: 'threshold', willRetry: false });
console.log(JSON.stringify(calls));
""".strip()
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(harness)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = json.loads(completed.stdout)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
