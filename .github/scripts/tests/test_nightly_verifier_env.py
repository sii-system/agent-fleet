import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
HARBOR_ENV = ROOT / "Agents/utils/common/Harbor/env.sh"


class NightlyVerifierEnvTest(unittest.TestCase):
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
