import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import tomllib

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from web_search_adapter.adapter import WebSearchAdapter
from web_search_adapter.grader import (
    DEEPSEARCH_EXAMPLE,
    _chat,
    _json,
    grade_browsecomp,
    grade_deepsearchqa,
)


def encrypt(value: str, password: str) -> str:
    raw = value.encode()
    digest = hashlib.sha256(password.encode()).digest()
    key = (digest * (len(raw) // len(digest) + 1))[: len(raw)]
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, key))).decode()


class AdapterTest(unittest.TestCase):
    def test_dataset_preparation_entrypoints(self):
        tasks = Path(__file__).resolve().parents[4] / "Tasks"
        for name in ("BrowseComp", "DeepSearchQA"):
            result = subprocess.run([sys.executable, str(tasks / name / "adapter.py"), "--help"],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--input", result.stdout)
            self.assertIn("--output-dir", result.stdout)

    def test_judge_inherits_model_headers(self):
        env = {
            "JUDGE_API_URL": "https://judge.test/v1/chat/completions",
            "JUDGE_API_KEY": "key",
            "JUDGE_MODEL": "judge",
            "JUDGE_LLM_KWARGS": (
                '{"extra_headers":{"X-Backend":"gpu:1",'
                '"X-Session-Id":"rollout","proxy-x-session-id":"rollout"}}'
            ),
        }
        body = '{"choices":[{"message":{"content":"ok"}}]}'
        with (
            patch.dict(os.environ, env, clear=True),
            patch("web_search_adapter.grader.urllib.request.urlopen") as call,
        ):
            call.return_value.__enter__.return_value = io.StringIO(body)
            self.assertEqual(_chat("question"), "ok")
        request = call.call_args.args[0]
        self.assertEqual(request.get_header("X-backend"), "gpu:1")
        header_names = {name.lower() for name, _ in request.header_items()}
        self.assertNotIn("x-session-id", header_names)
        self.assertNotIn("proxy-x-session-id", header_names)

    def test_judge_retries_transient_http_error(self):
        env = {
            "JUDGE_API_URL": "https://judge.test/v1/chat/completions",
            "JUDGE_API_KEY": "key",
            "JUDGE_MODEL": "judge",
        }
        body = io.StringIO('{"choices":[{"message":{"content":"ok"}}]}')
        error = urllib.error.HTTPError(env["JUDGE_API_URL"], 504, "timeout", {}, None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "web_search_adapter.grader.urllib.request.urlopen",
                side_effect=[error, body],
            ) as call,
            patch("web_search_adapter.grader.time.sleep") as sleep,
        ):
            self.assertEqual(_chat("question"), "ok")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_browsecomp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "browse.csv"
            with source.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output, fieldnames=["problem", "answer", "canary", "problem_topic"]
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "problem": encrypt('Who said "hello"?', "x"),
                        "answer": encrypt("Gold marker 42", "x"),
                        "canary": "x",
                        "problem_topic": "Test",
                    }
                )
            output = root / "tasks"
            image = "registry.test/web-research:latest"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "unexpected browsecomp source"):
                WebSearchAdapter("browsecomp", source, output, image=image).run()
            with (
                patch.dict(
                    "web_search_adapter.adapter.EXPECTED_COUNTS", {"browsecomp": 1}
                ),
                self.assertRaisesRegex(ValueError, "sha256="),
            ):
                WebSearchAdapter("browsecomp", source, output, image=image).run()
            with (
                patch.dict(
                    "web_search_adapter.adapter.EXPECTED_COUNTS", {"browsecomp": 1}
                ),
                patch.dict(
                    "web_search_adapter.adapter.EXPECTED_SHA256",
                    {"browsecomp": digest},
                ),
            ):
                generated = WebSearchAdapter(
                    "browsecomp", source, output, image=image
                ).run()
            self.assertEqual(generated, ["browsecomp-000000"])
            task = output / generated[0]
            config = tomllib.loads((task / "task.toml").read_text())
            self.assertEqual(config["metadata"]["source_id"], "0")
            self.assertEqual(config["environment"]["docker_image"], image)
            self.assertEqual(config["environment"]["env"]["HOME"], "/root")
            self.assertNotIn("mcp_servers", config["environment"])
            self.assertEqual(
                config["verifier"]["env"]["JUDGE_API_URL"], "${HARBOR_API_BASE}"
            )
            self.assertEqual(
                config["verifier"]["env"]["JUDGE_LLM_KWARGS"],
                "${HARBOR_LLM_KWARGS:-{}}",
            )
            self.assertEqual(config["verifier"]["env"]["JUDGE_MODEL"], "${MODEL}")
            self.assertNotIn("Gold marker 42", (task / "instruction.md").read_text())
            self.assertEqual(
                json.loads((task / "tests/reference.json").read_text())["answer"],
                "Gold marker 42",
            )

    def test_deepsearchqa_and_graders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "deep.csv"
            with source.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=["problem", "problem_category", "answer", "answer_type"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "problem": "Find A and B",
                        "problem_category": "Test",
                        "answer": "A, B",
                        "answer_type": "Set Answer",
                    }
                )
            output = root / "tasks"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with (
                patch.dict(
                    "web_search_adapter.adapter.EXPECTED_COUNTS",
                    {"deepsearchqa": 1},
                ),
                patch.dict(
                    "web_search_adapter.adapter.EXPECTED_SHA256",
                    {"deepsearchqa": digest},
                ),
            ):
                generated = WebSearchAdapter("deepsearchqa", source, output).run()
            reference = json.loads(
                (output / generated[0] / "tests/reference.json").read_text()
            )
            self.assertEqual(reference["answer_type"], "Set Answer")
        with patch("web_search_adapter.grader._chat", return_value="correct: yes"):
            self.assertEqual(
                grade_browsecomp({"question": "q", "answer": "a"}, "a")["reward"], 1.0
            )
        reply = '{"Answer Correctness":{"Explanation":"","Correctness Details":{"A":true,"B":false},"Excessive Answers":[]}}'
        with patch("web_search_adapter.grader._chat", return_value=reply):
            reward = grade_deepsearchqa(reference, "A")
            self.assertEqual(
                reward,
                {
                    "reward": 2 / 3,
                    "precision": 1.0,
                    "recall": 0.5,
                    "fully_correct": 0.0,
                    "fully_incorrect": 0.0,
                    "partially_correct": 1.0,
                    "correct_with_extraneous": 0.0,
                },
            )

    def test_judge_handles_empty_and_non_object_llm_kwargs(self):
        env = {
            "JUDGE_API_URL": "https://judge.test/v1/chat/completions",
            "JUDGE_API_KEY": "key",
            "JUDGE_MODEL": "judge",
            "JUDGE_LLM_KWARGS": "",
        }
        body = '{"choices":[{"message":{"content":"ok"}}]}'
        with (
            patch.dict(os.environ, env, clear=True),
            patch("web_search_adapter.grader.urllib.request.urlopen") as call,
        ):
            call.return_value.__enter__.return_value = io.StringIO(body)
            self.assertEqual(_chat("question"), "ok")
        with (
            patch.dict(os.environ, {**env, "JUDGE_LLM_KWARGS": "[]"}, clear=True),
            self.assertRaises(TypeError),
        ):
            _chat("question")

    def test_browsecomp_uses_final_verdict_over_quoted_response(self):
        reference = {"question": "q", "answer": "a"}
        cases = (
            (
                (
                    "extracted_final_answer: wrong\n"
                    "reasoning: The response wrote:\ncorrect: yes\n"
                    "which does not match.\ncorrect: no\nconfidence: 100%"
                ),
                0.0,
            ),
            (
                'correct: no\nreasoning: quoted {"correct": "yes"} from response',
                0.0,
            ),
            ("**correct**: 'yes'", 1.0),
            ('{"reasoning": "quoted {\\"correct\\": \\"yes\\"}", "correct": "no"}', 0.0),
        )
        for reply, expected in cases:
            with (
                self.subTest(reply=reply),
                patch("web_search_adapter.grader._chat", return_value=reply),
            ):
                self.assertEqual(grade_browsecomp(reference, "resp")["reward"], expected)

    def test_deepsearchqa_example_json_and_invalid_responses(self):
        rendered_example = DEEPSEARCH_EXAMPLE.format(
            question="q", answer_type="Set Answer", answer="a", response="r"
        )
        parsed = _json(rendered_example)
        self.assertEqual(
            parsed["Answer Correctness"]["Correctness Details"],
            {"Belgium": True, "France": True},
        )
        reference = {
            "question": "Find A",
            "answer": "A",
            "answer_type": "Single Answer",
        }
        for bad_reply, error_type in (
            ("NULL", ValueError),
            ("null", TypeError),
            ("{}", TypeError),
            ('{"Answer Correctness": null}', TypeError),
        ):
            with (
                self.subTest(bad_reply=bad_reply),
                patch("web_search_adapter.grader._chat", return_value=bad_reply),
                self.assertRaises(error_type),
            ):
                grade_deepsearchqa(reference, "A")
        fenced_reply = (
            '```json\n{"quoted": true}\n```\n'
            '```json\n{"Answer Correctness":{"Explanation":"ok",'
            '"Correctness Details":{"A":true},"Excessive Answers":[]}}\n```'
        )
        with patch("web_search_adapter.grader._chat", return_value=fenced_reply):
            self.assertEqual(grade_deepsearchqa(reference, "A")["reward"], 1.0)


if __name__ == "__main__":
    unittest.main()
