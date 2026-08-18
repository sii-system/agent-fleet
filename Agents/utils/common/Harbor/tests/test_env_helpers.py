from __future__ import annotations

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
    *args: str | Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ENV_PY), *(str(arg) for arg in args)],
        capture_output=True,
        env=environment,
        text=True,
        check=False,
    )


class _CacheHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"cache_schema=3\n" if self.path == "/manifest.txt" else b"ready\n"
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

        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(
            config["provider"]["custom"]["options"]["headers"],
            {"X-Route-Key": "deployment-a"},
        )

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
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(reachable.returncode, 0, reachable.stderr)
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        self.assertNotEqual(invalid_manifest.returncode, 0)


if __name__ == "__main__":
    unittest.main()
