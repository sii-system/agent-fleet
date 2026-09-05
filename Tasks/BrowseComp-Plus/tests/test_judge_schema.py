from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

BENCHMARK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK))
from judge.cache import evaluation_fingerprint, load_cached_evaluation  # noqa: E402
from judge.client import JudgeConfig  # noqa: E402
from judge.schema import scalar_result  # noqa: E402


class JudgeSchemaTest(unittest.TestCase):
    def test_normalizes_correctness_to_scalar_reward(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nested" / "q1_eval.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"judge_result": {"correct": True}, "retrieval": {"recall": 1}}), encoding="utf-8")
            result = scalar_result(Path(root), "q1")
            self.assertEqual(result["reward"], 1.0)
            self.assertTrue(result["correct"])

    def test_remote_judge_reuses_fleet_gateway_without_double_v1(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ",
            {
                "BROWSECOMP_JUDGE_MODE": "openai",
                "BASE_URL": "https://gateway.example.invalid/v1",
                "MODEL": "fleet-model",
            },
            clear=True,
        ):
            root_path = Path(root)
            config = JudgeConfig.from_env(root_path, root_path / "gold", root_path / "eval")
            command = config.command(root_path / "runs")
            self.assertEqual(config.model, "fleet-model")
            self.assertIn("https://gateway.example.invalid/v1", command)
            self.assertNotIn("https://gateway.example.invalid/v1/v1", command)
            self.assertIn("--source-root", command)

    def test_remote_judge_normalizes_completion_endpoint_to_api_root(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ",
            {
                "BROWSECOMP_JUDGE_MODE": "openai",
                "BASE_URL": "https://gateway.example.invalid/v1/chat/completions",
                "MODEL": "fleet-model",
            },
            clear=True,
        ):
            root_path = Path(root)
            config = JudgeConfig.from_env(root_path, root_path / "gold", root_path / "eval")
            command = config.command(root_path / "runs")
            self.assertEqual(
                command[command.index("--base_url") + 1],
                "https://gateway.example.invalid/v1",
            )

    def test_remote_judge_bypasses_proxy_for_fleet_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ",
            {
                "BROWSECOMP_JUDGE_MODE": "openai",
                "BASE_URL": "https://gateway.example.invalid/v1",
                "MODEL": "fleet-model",
                "NO_PROXY": "existing.example",
            },
            clear=True,
        ), patch("judge.client.subprocess.run") as run:
            root_path = Path(root)
            config = JudgeConfig.from_env(root_path, root_path / "gold", root_path / "eval")
            config.evaluate(root_path / "runs")
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(
                child_env["NO_PROXY"],
                "existing.example,gateway.example.invalid",
            )
            self.assertEqual(child_env["no_proxy"], child_env["NO_PROXY"])
            self.assertEqual(child_env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_local_judge_does_not_inherit_agent_model(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "os.environ",
            {"BROWSECOMP_JUDGE_MODE": "local", "MODEL": "remote-agent-model"},
            clear=True,
        ):
            root_path = Path(root)
            config = JudgeConfig.from_env(root_path, root_path / "gold", root_path / "eval")
            self.assertEqual(config.model, "Qwen/Qwen3-32B")

    def test_evaluation_cache_requires_matching_inputs_and_judge_config(self) -> None:
        run = {
            "query_id": "q1",
            "status": "completed",
            "result": [{"type": "output_text", "output": "answer"}],
        }
        fingerprint = evaluation_fingerprint(
            run=run,
            prompt="grader prompt",
            relevant_docids={"doc-2", "doc-1"},
            model="judge-model",
            base_url="https://judge.example.invalid/v1",
            api_mode="chat-completions",
            max_output_tokens=1024,
        )
        self.assertEqual(
            fingerprint,
            evaluation_fingerprint(
                run={"result": run["result"], "status": "completed", "query_id": "q1"},
                prompt="grader prompt",
                relevant_docids={"doc-1", "doc-2"},
                model="judge-model",
                base_url="https://judge.example.invalid/v1",
                api_mode="chat-completions",
                max_output_tokens=1024,
            ),
        )
        self.assertNotEqual(
            fingerprint,
            evaluation_fingerprint(
                run=run,
                prompt="changed prompt",
                relevant_docids={"doc-1", "doc-2"},
                model="judge-model",
                base_url="https://judge.example.invalid/v1",
                api_mode="chat-completions",
                max_output_tokens=1024,
            ),
        )
        self.assertNotEqual(
            fingerprint,
            evaluation_fingerprint(
                run=run,
                prompt="grader prompt",
                relevant_docids={"doc-1", "doc-2"},
                model="different-judge-model",
                base_url="https://judge.example.invalid/v1",
                api_mode="chat-completions",
                max_output_tokens=1024,
            ),
        )

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "q1_eval.json"
            path.write_text(
                json.dumps(
                    {
                        "judge_result": {"correct": True},
                        "evaluation_cache": {
                            "schema_version": 1,
                            "fingerprint": fingerprint,
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(load_cached_evaluation(path, fingerprint))
            self.assertIsNone(load_cached_evaluation(path, "sha256:stale"))
            path.write_text(json.dumps({"judge_result": {"correct": True}}))
            self.assertIsNone(load_cached_evaluation(path, fingerprint))

    def test_openai_evaluator_reuses_only_matching_cache_entries(self) -> None:
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = Mock  # type: ignore[attr-defined]
        module_path = BENCHMARK / "judge" / "evaluate_openai.py"
        spec = importlib.util.spec_from_file_location(
            "browsecomp_test_evaluate_openai", module_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        with patch.dict(sys.modules, {"openai": fake_openai}):
            spec.loader.exec_module(module)  # type: ignore[union-attr]

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            input_dir = root_path / "runs"
            eval_dir = root_path / "eval"
            input_dir.mkdir()
            ground_truth = root_path / "gold.jsonl"
            qrels = root_path / "qrels.txt"
            source_root = root_path / "source"
            source_root.mkdir()
            ground_truth.write_text(
                json.dumps(
                    {"query_id": "q1", "query": "question", "answer": "answer"}
                )
                + "\n",
                encoding="utf-8",
            )
            qrels.write_text("q1 0 doc-1 1\n", encoding="utf-8")
            (input_dir / "q1.json").write_text(
                json.dumps(
                    {
                        "query_id": "q1",
                        "status": "completed",
                        "result": [
                            {"type": "output_text", "output": "answer [doc-1]"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "evaluate_openai.py",
                "--input_dir",
                str(input_dir),
                "--ground_truth",
                str(ground_truth),
                "--eval_dir",
                str(eval_dir),
                "--qrel_evidence",
                str(qrels),
                "--source-root",
                str(source_root),
                "--model",
                "judge-a",
            ]
            judge = Mock(
                return_value=(
                    "extracted_final_answer: answer\nreasoning: match\n"
                    "correct: yes\nconfidence: 100"
                )
            )
            with (
                patch.object(sys, "argv", argv),
                patch.dict(os.environ, {"API_KEY": "fixture-key"}, clear=True),
                patch.object(
                    module,
                    "load_grader_template",
                    return_value="{question}|{response}|{correct_answer}",
                ),
                patch.object(module, "call_judge", judge),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(module.main(), 0)
                self.assertEqual(module.main(), 0)
                self.assertEqual(judge.call_count, 1)
                argv[argv.index("judge-a")] = "judge-b"
                self.assertEqual(module.main(), 0)
                self.assertEqual(judge.call_count, 2)


if __name__ == "__main__":
    unittest.main()
