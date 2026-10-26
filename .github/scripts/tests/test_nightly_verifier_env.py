import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[3]
HARBOR_ENV = ROOT / "Agents/utils/common/Harbor/env.sh"
SPEC = importlib.util.spec_from_file_location(
    "nightly_model_preflight", ROOT / ".github/scripts/harbor_nightly_preflight.py"
)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class NightlyVerifierEnvTest(unittest.TestCase):
    def test_run_settings_reach_native_verifier_and_simulator_manifests(self):
        for benchmark, template, expected_models in (
            ("scale-ai/swe-atlas-qna", {
                "EVAL_API_KEY": "${OPENAI_API_KEY}",
                "EVAL_BASE_URL": "${OPENAI_API_BASE}",
                "EVAL_MODEL": "${EVAL_MODEL:-anthropic/claude-opus-4-5-20251101}",
            }, {"EVAL_MODEL": "run-model"}),
            ("sierra-research/tau3-bench", {
                "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                "OPENAI_BASE_URL": "${OPENAI_BASE_URL}",
                "TAU2_USER_MODEL": "${TAU2_USER_MODEL:-gpt-5.2}",
                "TAU2_NL_ASSERTIONS_MODEL": "${TAU2_NL_ASSERTIONS_MODEL:-gpt-5.2}",
                "TAU2_USER_LLM_ARGS_JSON": "${TAU2_USER_LLM_ARGS_JSON:-}",
            }, {"TAU2_USER_MODEL": "openai/run-model",
                "TAU2_NL_ASSERTIONS_MODEL": "openai/run-model"}),
        ):
            with self.subTest(benchmark=benchmark), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "github-env"
                run_env = {
                    "MODEL": "run-model", "API_KEY": "fake-run-key",
                    "BASE_URL_RAW": "https://run.invalid/v1/chat/completions",
                    "GITHUB_ENV": str(target),
                }
                with (
                    mock.patch.dict(os.environ, run_env, clear=True),
                    mock.patch.object(preflight, "probe"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(preflight.main(["--benchmark", benchmark]), 0)
                exported = dict(line.split("=", 1) for line in target.read_text().splitlines())
                script = """
source "$HARBOR_TEST_ENV"
"$HARBOR_TEST_PYTHON" - <<'PY'
import json, os
from harbor.utils.env import resolve_env_vars
print(json.dumps(resolve_env_vars(json.loads(os.environ['HARBOR_TEST_MANIFEST']))))
PY
"""
                result = subprocess.run(
                    ["bash", "-c", script], cwd=ROOT, text=True, capture_output=True,
                    check=False, env={
                        "PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                        "HARBOR_TEST_ENV": str(HARBOR_ENV),
                        "HARBOR_TEST_PYTHON": sys.executable,
                        "HARBOR_TEST_MANIFEST": json.dumps(template),
                        "AGENT_FLEET_CONFIG_LOADED_ROOT": str(ROOT),
                        "AGENT_FLEET_PATHS_FILE": f"{tmp}/missing.env",
                        "AGENT_FLEET_RUNTIME_DIR": f"{tmp}/runtime",
                        "BASE_URL": "https://run.invalid/v1", "AGENT": "claude-code",
                        "MODEL": "run-model", "API_KEY": "fake-run-key", "OPIK_URL": "",
                        **exported,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                resolved = json.loads(next(
                    line for line in result.stdout.splitlines() if line.startswith("{")
                ))
                self.assertEqual({k: resolved[k] for k in expected_models}, expected_models)
                if benchmark == "scale-ai/swe-atlas-qna":
                    self.assertEqual(resolved["EVAL_API_KEY"], "fake-run-key")
                    self.assertEqual(resolved["EVAL_BASE_URL"], "https://run.invalid/v1")
                else:
                    self.assertEqual(resolved["OPENAI_API_KEY"], "fake-run-key")
                    self.assertEqual(resolved["OPENAI_BASE_URL"], "https://run.invalid/v1")
                    self.assertEqual(json.loads(resolved["TAU2_USER_LLM_ARGS_JSON"]), {})

    def test_both_nightlies_resolve_swe_atlas_verifier_credentials(self):
        for workflow in ("harbor-self-hosted-nightly.yml", "harbor-e2e-validation.yml"):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as tmp:
                data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
                job = next(iter(data["jobs"].values()))
                step = next(s for s in job["steps"] if s.get("id") == "harbor")
                # Replace the timed benchmark with Harbor's real environment
                # loader and placeholder resolver; no containers or API calls.
                script = """
timeout() {
  (
    source "$HARBOR_TEST_ENV"
    "$HARBOR_TEST_PYTHON" - <<'PY'
import json
from harbor.utils.env import resolve_env_vars
print(json.dumps(resolve_env_vars({
    "OPENAI_API_BASE": "${OPENAI_API_BASE}",
    "OPENAI_API_KEY": "${OPENAI_API_KEY}",
})))
PY
  )
}
""" + step["run"]
                result = subprocess.run(
                    ["bash", "-c", script], cwd=ROOT, text=True, capture_output=True,
                    check=False,
                    env={
                        "PATH": os.environ["PATH"], "HOME": tmp,
                        "HARBOR_TEST_ENV": str(HARBOR_ENV),
                        "HARBOR_TEST_PYTHON": sys.executable,
                        "AGENT_FLEET_CONFIG_LOADED_ROOT": str(ROOT),
                        "AGENT_FLEET_PATHS_FILE": f"{tmp}/missing.env",
                        "AGENT_FLEET_RUNTIME_DIR": f"{tmp}/runtime",
                        "BASE_URL_RAW": "https://gateway.invalid/v1/chat/completions",
                        "API_KEY": "fake-nightly-key", "OPIK_URL": "",
                        "AGENT": "claude-code", "BENCHMARK": "scale-ai/swe-atlas-qna",
                        "WORKERS": "1", "TOTAL_WORKERS": "1", "TASKS": "",
                        "OUTPUT_PATH": f"{tmp}/run", "RUNNER_TEMP": tmp,
                        "GITHUB_OUTPUT": f"{tmp}/output", "BENCHMARK_TIMEOUT": "1m",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (Path(tmp) / "output").read_text(), "status=0\n", result.stderr
                )
                resolved = json.loads(next(
                    line for line in result.stdout.splitlines() if line.startswith("{")
                ))
                self.assertEqual(resolved, {
                    "OPENAI_API_BASE": "https://gateway.invalid/v1",
                    "OPENAI_API_KEY": "fake-nightly-key",
                })


if __name__ == "__main__":
    unittest.main()
