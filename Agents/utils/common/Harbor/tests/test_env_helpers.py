from __future__ import annotations

import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARBOR_DIR = Path(__file__).parents[1]
ENV_PY = HARBOR_DIR / "env.py"


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

            result = subprocess.run(
                ["python3", str(ENV_PY), "generate-task-file", str(dataset), str(output)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "text-task\nyaml-task\n")

    def test_filter_task_file_preserves_requested_order_and_rejects_missing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.txt"
            output = root / "selected.txt"
            source.write_text("task-a\ntask-b\ntask-c\n")

            result = subprocess.run(
                [
                    "python3",
                    str(ENV_PY),
                    "filter-task-file",
                    str(source),
                    str(output),
                    "task-c, task-a, task-c",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "task-c\ntask-a\n")

            missing = subprocess.run(
                [
                    "python3",
                    str(ENV_PY),
                    "filter-task-file",
                    str(source),
                    str(root / "missing.txt"),
                    "missing-a,task-a,missing-b",
                ],
                capture_output=True,
                text=True,
                check=False,
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

            ready = subprocess.run(
                ["python3", str(ENV_PY), "tar-file-ready", str(valid)],
                capture_output=True,
                text=True,
                check=False,
            )
            not_ready = subprocess.run(
                ["python3", str(ENV_PY), "tar-file-ready", str(invalid)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertNotEqual(not_ready.returncode, 0)

    def test_url_helpers_check_reachability_and_manifest_schema(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CacheHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            reachable = subprocess.run(
                ["python3", str(ENV_PY), "url-reachable", f"{base_url}/agent.tgz"],
                capture_output=True,
                text=True,
                check=False,
            )
            manifest = subprocess.run(
                ["python3", str(ENV_PY), "manifest-url-ready", f"{base_url}/manifest.txt"],
                capture_output=True,
                text=True,
                check=False,
            )
            invalid_manifest = subprocess.run(
                ["python3", str(ENV_PY), "manifest-url-ready", f"{base_url}/agent.tgz"],
                capture_output=True,
                text=True,
                check=False,
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
