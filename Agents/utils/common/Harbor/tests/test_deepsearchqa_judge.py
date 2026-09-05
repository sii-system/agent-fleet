import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Self
from unittest.mock import patch

VERIFIER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deepsearchqa_verifier_files"
    / "openai_judge.py"
)
SPEC = importlib.util.spec_from_file_location("deepsearchqa_judge", VERIFIER_PATH)
assert SPEC and SPEC.loader
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)


def load_verifier_adapter() -> ModuleType:
    class FakeVerifier:
        def __init__(
            self,
            *_args: object,
            verifier_env: dict[str, str] | None = None,
            **_kwargs: object,
        ) -> None:
            self.verifier_env = verifier_env

        def _resolve_tests(self) -> tuple[list[Path], Path, Path]:
            official = Path("/official/tests")
            return [official], official, official / "test.sh"

    harbor = ModuleType("harbor")
    harbor_verifier = ModuleType("harbor.verifier")
    harbor_verifier_module = ModuleType("harbor.verifier.verifier")
    harbor_verifier_module.Verifier = FakeVerifier
    adapter_path = Path(__file__).resolve().parents[1] / "deepsearchqa_verifier.py"
    adapter_spec = importlib.util.spec_from_file_location(
        "deepsearchqa_verifier_adapter_test",
        adapter_path,
    )
    assert adapter_spec and adapter_spec.loader
    adapter = importlib.util.module_from_spec(adapter_spec)
    with patch.dict(
        sys.modules,
        {
            "harbor": harbor,
            "harbor.verifier": harbor_verifier,
            "harbor.verifier.verifier": harbor_verifier_module,
        },
    ):
        adapter_spec.loader.exec_module(adapter)
    return adapter


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class DeepSearchQAJudgeTest(unittest.TestCase):
    def test_adapter_injects_judge_env_and_overlays_official_tests(self) -> None:
        adapter = load_verifier_adapter()
        with patch.dict(
            "os.environ",
            {
                "JUDGE_BASE_URL": "https://judge.example/v1",
                "JUDGE_API_KEY": "secret",
                "JUDGE_MODEL": "judge-model",
            },
            clear=True,
        ):
            verifier = adapter.DeepSearchQAVerifier(
                verifier_env={"EXISTING": "value"}
            )

        self.assertEqual(
            verifier.verifier_env,
            {
                "EXISTING": "value",
                "JUDGE_BASE_URL": "https://judge.example/v1",
                "JUDGE_API_KEY": "secret",
                "JUDGE_MODEL": "judge-model",
            },
        )
        source_dirs, tests_dir, test_path = verifier._resolve_tests()
        self.assertEqual(source_dirs[0], Path("/official/tests"))
        self.assertEqual(source_dirs[-1], tests_dir)
        self.assertEqual(tests_dir.name, "deepsearchqa_verifier_files")
        self.assertEqual(test_path, tests_dir / "test.sh")

    def test_wrapper_replaces_only_the_official_judge_call(self) -> None:
        official_verifier = ModuleType("verifier")
        invoked: list[object] = []
        official_verifier.call_gemini = object()

        def official_main() -> None:
            invoked.append(official_verifier.call_gemini)

        official_verifier.main = official_main
        with patch.dict(sys.modules, {"verifier": official_verifier}):
            VERIFIER.main()

        self.assertEqual(invoked, [VERIFIER.call_judge])

    def test_normalizes_openai_compatible_endpoint(self) -> None:
        cases = {
            "https://judge.example": "https://judge.example/v1/chat/completions",
            "https://judge.example/v1": "https://judge.example/v1/chat/completions",
            "https://judge.example/v1/chat/completions": (
                "https://judge.example/v1/chat/completions"
            ),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(VERIFIER.judge_endpoint(value), expected)

    def test_calls_openai_chat_completions_contract(self) -> None:
        response = FakeResponse(
            {"choices": [{"message": {"content": '{"Answer Correctness": {}}'}}]}
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "JUDGE_BASE_URL": "https://judge.example/v1/chat/completions",
                    "JUDGE_API_KEY": "secret",
                    "JUDGE_MODEL": "judge-model",
                },
                clear=True,
            ),
            patch.object(VERIFIER.request, "urlopen", return_value=response) as urlopen,
        ):
            result = VERIFIER.call_judge("rate this")

        self.assertEqual(result, '{"Answer Correctness": {}}')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://judge.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "judge-model")
        self.assertEqual(
            payload["messages"], [{"role": "user", "content": "rate this"}]
        )

    def test_requires_all_judge_settings(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "JUDGE_BASE_URL"),
        ):
            VERIFIER.call_judge("rate this")

    def test_rejects_malformed_chat_completion(self) -> None:
        response = FakeResponse({"choices": []})
        with (
            patch.dict(
                "os.environ",
                {
                    "JUDGE_BASE_URL": "https://judge.example/v1",
                    "JUDGE_API_KEY": "secret",
                    "JUDGE_MODEL": "judge-model",
                },
                clear=True,
            ),
            patch.object(VERIFIER.request, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "malformed"),
        ):
            VERIFIER.call_judge("rate this")


if __name__ == "__main__":
    unittest.main()
