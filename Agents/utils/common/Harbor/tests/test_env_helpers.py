from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARBOR_DIR = Path(__file__).parents[1]
ENV_PY = HARBOR_DIR / "env.py"


def run_env_helper(
    *args: str | Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if environment is None:
        environment = os.environ.copy()
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["no_proxy"] = environment["NO_PROXY"]

    return subprocess.run(
        ["python3", str(ENV_PY), *(str(arg) for arg in args)],
        capture_output=True,
        env=environment,
        text=True,
        check=False,
    )


def run_env_helper_env(
    *args: str | Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]

    if extra_env:
        env.update(extra_env)

    return run_env_helper(*args, environment=env)


class _CacheHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = (
            b"cache_schema=3\npi_runtime_version=0.81.1\n"
            if self.path == "/manifest.txt"
            else b"ready\n"
        )
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class HarborEnvHelperTests(unittest.TestCase):
    def test_opencode_config_applies_model_request_headers(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "HARBOR_ANTHROPIC_BASE_URL": "http://model.example",
                "HARBOR_ANTHROPIC_AUTH_TOKEN": "test-key",
                "HARBOR_MODEL": "custom/test-model",
                "HARBOR_LLM_KWARGS": (
                    '{"extra_headers":{"X-Route-Key":"deployment-a"}}'
                ),
            }
        )

        result = run_env_helper("opencode-config", environment=environment)
        runtime_result = run_env_helper(
            "opencode-runtime-secrets", environment=environment
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runtime_result.returncode, 0, runtime_result.stderr)
        config = json.loads(result.stdout)
        runtime_secrets = json.loads(runtime_result.stdout)
        options = config["provider"]["custom"]["options"]
        self.assertRegex(
            options["apiKey"],
            r"^\{env:AGENT_FLEET_OPENCODE_SECRET_[0-9A-F]{16}\}$",
        )
        self.assertRegex(
            options["headers"]["X-Route-Key"],
            r"^\{env:AGENT_FLEET_OPENCODE_SECRET_[0-9A-F]{16}\}$",
        )
        self.assertEqual(
            set(runtime_secrets.values()),
            {"test-key", "deployment-a"},
        )
        self.assertNotIn("test-key", result.stdout)
        self.assertNotIn("deployment-a", result.stdout)

    def test_generate_task_file_lists_supported_tasks_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset"
            output = root / "tasks.txt"
            (dataset / "yaml-task").mkdir(parents=True)
            (dataset / "yaml-task" / "task.yaml").write_text("version: 1\n")
            (dataset / "text-task").mkdir()
            (dataset / "text-task" / "instruction.md").write_text("Do the task.\n")
            (dataset / "empty-task").mkdir()
            (dataset / "empty-task" / "instruction.md").write_text("\n")
            (dataset / "not-a-task").mkdir()
            (dataset / "not-a-task" / "README.md").write_text("not a task\n")
            (dataset / "not-a-directory").write_text("ignore\n")

            result = run_env_helper("generate-task-file", dataset, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "text-task\nyaml-task\n")

    def test_filter_task_file_preserves_requested_order_and_rejects_missing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.txt"
            output = root / "selected.txt"
            source.write_text("task-a\ntask-b\ntask-c\n")

            result = run_env_helper(
                "filter-task-file",
                source,
                output,
                "task-c, task-a, task-c",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "task-c\ntask-a\n")

            missing = run_env_helper(
                "filter-task-file",
                source,
                root / "missing.txt",
                "missing-a,task-a,missing-b",
            )

            self.assertEqual(missing.returncode, 2)
            self.assertIn("unknown task(s): missing-a, missing-b", missing.stderr)
            self.assertFalse((root / "missing.txt").exists())

    def test_tar_file_ready_accepts_valid_archives_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "runtime.tar.xz"
            invalid = root / "invalid.tar.xz"
            with tarfile.open(valid, "w:xz") as archive:
                archive.addfile(tarfile.TarInfo("runtime/bin/python"))
            invalid.write_text("not a tar archive")

            ready = run_env_helper("tar-file-ready", valid)
            not_ready = run_env_helper("tar-file-ready", invalid)

            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertNotEqual(not_ready.returncode, 0)

    def test_npm_tarball_version_ready_requires_exact_embedded_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "package.tgz"
            package_json = json.dumps(
                {"name": "@anthropic-ai/claude-code", "version": "1.2.3"}
            ).encode()
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("package/package.json")
                member.size = len(package_json)
                archive.addfile(member, io.BytesIO(package_json))

            matching = run_env_helper(
                "npm-tarball-version-ready", archive_path, "1.2.3"
            )
            mismatched = run_env_helper(
                "npm-tarball-version-ready", archive_path, "9.9.9"
            )
            tagged = run_env_helper(
                "npm-tarball-version-ready", archive_path, "latest"
            )

            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertNotEqual(mismatched.returncode, 0)
            self.assertEqual(tagged.returncode, 0, tagged.stderr)

    def test_portable_tar_removes_external_xz_runtime_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = root / "node"
            payload.write_text("fixture-node\n", encoding="utf-8")
            compressed = root / "node-runtime.tar.xz"
            portable = root / "pi-node-runtime.tar.gz"
            with tarfile.open(compressed, "w:xz") as archive:
                archive.add(payload, arcname="node-runtime/bin/node")

            result = run_env_helper("portable-tar", compressed, portable)

            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(portable, "r:gz") as archive:
                member = archive.extractfile("node-runtime/bin/node")
                self.assertIsNotNone(member)
                assert member is not None
                self.assertEqual(member.read(), b"fixture-node\n")

    def test_url_helpers_check_reachability_and_manifest_schema(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CacheHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            reachable = run_env_helper("url-reachable", f"{base_url}/agent.tgz")
            manifest = run_env_helper(
                "manifest-url-ready", f"{base_url}/manifest.txt"
            )
            invalid_manifest = run_env_helper(
                "manifest-url-ready", f"{base_url}/agent.tgz"
            )
            selected_version = run_env_helper(
                "manifest-url-ready",
                f"{base_url}/manifest.txt",
                "pi_runtime_version=0.81.1",
            )
            missing_version = run_env_helper(
                "manifest-url-ready",
                f"{base_url}/manifest.txt",
                "pi_runtime_version=0.82.0",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(reachable.returncode, 0, reachable.stderr)
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        self.assertEqual(selected_version.returncode, 0, selected_version.stderr)
        self.assertNotEqual(invalid_manifest.returncode, 0)
        self.assertNotEqual(missing_version.returncode, 0)

    def _pi_models_config(
        self, base_url: str, max_tokens: str = ""
    ) -> subprocess.CompletedProcess[str]:
        extra = {
            "PI_PROVIDER": "sii-gateway",
            "HARBOR_MODEL": "sii-gateway/fake-model",
            "BASE_URL": base_url,
        }
        if max_tokens:
            extra["HARBOR_MAX_TOKENS"] = max_tokens
        return run_env_helper_env("pi-models-config", extra_env=extra)

    def test_pi_models_config_appends_v1_to_bare_root(self) -> None:
        result = self._pi_models_config("https://gateway.example:8443")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        provider = payload["providers"]["sii-gateway"]
        self.assertEqual(
            provider["baseUrl"], "https://gateway.example:8443/v1"
        )
        self.assertEqual(provider["models"][0]["maxTokens"], 32768)

    def test_pi_models_config_keeps_existing_v1_root(self) -> None:
        result = self._pi_models_config("https://gateway.example:8443/v1")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["baseUrl"],
            "https://gateway.example:8443/v1",
        )

    def test_pi_models_config_applies_max_tokens(self) -> None:
        result = self._pi_models_config(
            "https://gateway.example:8443", max_tokens="16384"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["models"][0]["maxTokens"],
            16384,
        )

    def test_pi_models_config_honors_context_window_override(self) -> None:
        result = run_env_helper_env(
            "pi-models-config",
            extra_env={
                "PI_PROVIDER": "sii-gateway",
                "HARBOR_MODEL": "sii-gateway/fake-model",
                "BASE_URL": "https://gateway.example:8443",
                "PI_CONTEXT_WINDOW": "65536",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["models"][0]["contextWindow"],
            65536,
        )

    def test_pi_models_config_defaults_context_window(self) -> None:
        result = self._pi_models_config("https://gateway.example:8443")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["models"][0]["contextWindow"],
            204800,
        )

    def test_pi_models_config_rejects_invalid_context_window(self) -> None:
        result = self._pi_models_config("https://gateway.example:8443")
        result = run_env_helper_env(
            "pi-models-config",
            extra_env={
                "PI_PROVIDER": "sii-gateway",
                "HARBOR_MODEL": "sii-gateway/fake-model",
                "BASE_URL": "https://gateway.example:8443",
                "PI_CONTEXT_WINDOW": "big",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PI_CONTEXT_WINDOW must be a positive integer", result.stderr)

    def test_pi_models_config_rejects_non_v1_path(self) -> None:
        result = self._pi_models_config("https://gateway.example/regions/us-east")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/v1 API root", result.stderr)

    def test_pi_models_config_rejects_relative_base_url(self) -> None:
        result = self._pi_models_config("gateway.example:8443")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute URL", result.stderr)

    def test_pi_models_config_uses_rollout_max_budget(self) -> None:
        result = run_env_helper_env(
            "pi-models-config",
            extra_env={
                "PI_PROVIDER": "sii-gateway",
                "HARBOR_MODEL": "sii-gateway/fake-model",
                "BASE_URL": "https://gateway.example:8443",
                "ROLLOUT": "1",
                "RL_MAX_NEW_TOKENS": "2000",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["models"][0]["maxTokens"],
            2000,
        )

    def test_pi_models_config_keeps_benchmark_default_with_harbor_max_set(self) -> None:
        result = run_env_helper_env(
            "pi-models-config",
            extra_env={
                "PI_PROVIDER": "sii-gateway",
                "HARBOR_MODEL": "sii-gateway/fake-model",
                "BASE_URL": "https://gateway.example:8443",
                "HARBOR_MAX_NEW_TOKENS": "65536",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["providers"]["sii-gateway"]["models"][0]["maxTokens"],
            32768,
        )

    def test_pi_models_config_derives_provider_from_base_url(self) -> None:
        result = run_env_helper_env(
            "pi-models-config",
            extra_env={
                "HARBOR_MODEL": "fake-model",
                "BASE_URL": "https://gateway.example.com:8443/v1",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("gateway.example.com", payload["providers"])
        self.assertEqual(
            payload["providers"]["gateway.example.com"]["baseUrl"],
            "https://gateway.example.com:8443/v1",
        )

    def test_pi_models_config_rejects_non_numeric_max_tokens(self) -> None:
        result = self._pi_models_config(
            "https://gateway.example", max_tokens="8192k"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive integer", result.stderr)

    def test_pi_models_config_rejects_non_positive_max_tokens(self) -> None:
        result = self._pi_models_config(
            "https://gateway.example", max_tokens="0"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive integer", result.stderr)


if __name__ == "__main__":
    unittest.main()
