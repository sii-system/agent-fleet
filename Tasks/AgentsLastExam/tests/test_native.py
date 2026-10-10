"""Exercise a real Harbor trial and the pinned ALE driver via loopback CUA."""

import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from ale_adapter.source import REVISION, source_digest
from harbor.environments.base import ExecResult
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from kubevirt_windows.control import Cluster, Settings
from kubevirt_windows.environment import KubeVirtWindowsEnvironment

SOURCE = os.environ.get("ALE_TEST_SOURCE")
PYTHON = os.environ.get("ALE_TEST_PYTHON")


@unittest.skipUnless(SOURCE and PYTHON, "Prepare the pinned native ALE source and Python environment")
class NativeTrialTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_native_trial(self):
        await self.native_trial("windows")

    async def test_linux_native_trial(self):
        await self.native_trial("linux")

    async def test_linux_sbx_native_trial(self):
        await self.native_trial("linux", backend_mode="sbx")

    async def test_linux_setup_failure_cleans_sandbox(self):
        await self.native_trial("linux", fail_setup=True)

    async def test_linux_setup_timeout_cleans_sandbox(self):
        await self.native_trial("linux", cancel_setup=True)

    async def native_trial(self, os_type, fail_setup=False, cancel_setup=False, backend_mode="docker"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            await asyncio.to_thread(subprocess.run,
                                    ["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--shared", SOURCE, str(source)],
                                    check=True)
            await asyncio.to_thread(subprocess.run,
                                    ["git", "-C", str(source), "checkout", "--quiet", "--detach", REVISION], check=True)
            fixture = source / "tasks/fixture/roundtrip"
            fixture.mkdir(parents=True)
            windows = os_type == "windows"
            guest_root = "C:/fixture" if windows else "/home/user/fixture"
            reference = ("E:/agenthle" if windows else "/media/user/data/agenthle") + "/fixture/roundtrip/base/reference/expected.txt"
            config_import = "tasks.common_config import GeneralTaskConfig" if windows else "tasks.linux_runtime import LinuxTaskConfig as GeneralTaskConfig"
            (fixture / "main.py").write_text(f'''
import asyncio
import cua_bench as cb
from {config_import}
config = GeneralTaskConfig(DOMAIN_NAME="fixture", TASK_NAME="roundtrip", VARIANT_NAME="base")
def load():
    return [cb.Task(description="Write a fractional answer", metadata=config.to_metadata(),
                    computer={{"setup_config": {{"os_type": "{os_type}"}}}})]
async def start(task, session):
    {"raise RuntimeError('fixture setup failed')" if fail_setup else "pass"}
    await session.write_bytes("{guest_root}/setup.txt", b"ready")
    {"await asyncio.sleep(3600)" if cancel_setup else "pass"}
async def evaluate(task, session):
    assert await session.read_bytes("{guest_root}/setup.txt") == b"ready"
    assert await session.read_bytes("{reference}") == b"reference"
    return [float(await session.read_bytes("{guest_root}/answer.txt")), 0.9]
''')
            (fixture / "task_card.json").write_text(json.dumps({"vm": {"snapshot": "cpu-free" if windows else "cpu-free-ubuntu", "timeout": 7200}}))
            task = root / "task"
            (task / "environment").mkdir(parents=True)
            (task / "tests").mkdir()
            (task / "instruction.md").write_text("Write a fractional answer")
            (task / "task.toml").write_text(
                f'[environment]\nos="{os_type}"\ncpus=4\nmemory_mb=16384\nbuild_timeout_sec={10 if cancel_setup else 60}\n'
                '[agent]\ntimeout_sec=60\n[verifier]\ntimeout_sec=60\n'
            )
            (task / "tests/test.bat").write_bytes(b"@echo off\r\nexit /b 1\r\n")
            (task / "tests/test.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 1\n")
            native = {"task": "tasks/fixture/roundtrip", "variant": 0, "os": os_type, "cpus": 4, "memory_mb": 16384,
                      "snapshot": "cpu-free" if windows else "cpu-free-ubuntu",
                      "image_family": "ale-win10" if windows else "ale-ubuntu22", "requires_gpu": False,
                      "resolution": [1024, 768], "revision": REVISION, "source_sha256": source_digest(source)}
            (task / "environment/ale.json").write_text(json.dumps(native))
            mapping = root / "images.json"
            mapping.write_text(json.dumps({
                "cpu-free": {"pvc": "ale-cpu-free", "image_family": "ale-win10"},
                "cpu-free-ubuntu": {"image_family": "ale-ubuntu22", "docker_image": "example/ale:fixture", "cua_port": 5000}}))
            files, phases = {}, []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    method, params = request["command"], request["params"]
                    result = {"success": True}
                    if method == "run_command":
                        command = params["command"]
                        result.update(return_code=0, stdout="", stderr="")
                        if "base64.b64decode" in command:
                            phases.append("resolution")
                            result["stdout"] = "set_ok"
                        elif "7z x" in command:
                            phases.append("reference")
                            files[reference] = b"reference"
                        elif command.startswith("base64 -d "):
                            parts = shlex.split(command)
                            files[parts[4]] = base64.b64decode(files[parts[2]])
                        elif "find . -mindepth" in command:
                            directory = "/logs/agent/"
                            result["stdout"] = "\n".join(f"{path.removeprefix(directory)}\tf\t{len(data)}"
                                for path, data in files.items() if path.startswith(directory) and "'/logs/agent'" in command)
                        elif "bash /logs/agent/run.sh" in command:
                            assert files["/logs/agent/instruction.txt"] == b"Write a fractional answer"
                            assert files[f"{guest_root}/setup.txt"] == b"ready"
                            assert "reference" not in phases
                            phases.append("agent")
                            files[f"{guest_root}/answer.txt"] = b"0.4"
                            files["/logs/agent/stdout.txt"] = b"agent executed"
                            files["/logs/agent/stderr.txt"] = b""
                        elif command == "fixture-release":
                            phases.append("cleanup")
                        elif "pyvenv.cfg" in command:
                            result["return_code"] = 1
                    elif method == "write_bytes":
                        files[params["path"]] = base64.b64decode(params["content_b64"])
                        if params["path"] == f"{guest_root}/setup.txt":
                            phases.append("setup")
                    elif method == "write_text":
                        files[params["path"]] = params["content"].encode()
                    elif method == "get_file_size":
                        result["size"] = len(files[params["path"]])
                    elif method == "read_bytes":
                        result["content_b64"] = base64.b64encode(files[params["path"]]).decode()
                        if params["path"] == reference:
                            phases.append("grade")
                    else:
                        result = {"success": False, "error": "Unsupported fixture command"}
                    body = ("data: " + json.dumps(result) + "\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoint = f"http://127.0.0.1:{server.server_port}"
            transport = Mock(client=Mock(base_url=endpoint))
            transport.upload_file = AsyncMock()
            transport.list_files = AsyncMock(return_value=[])

            async def execute(*args, **kwargs):
                self.assertEqual(files[f"{guest_root}/setup.txt"], b"ready")
                self.assertNotIn("reference", phases)
                phases.append("agent")
                files[f"{guest_root}/answer.txt"] = b"0.4"
                return {"return_code": 0, "stdout": "", "stderr": ""}
            transport.execute = AsyncMock(side_effect=execute)

            async def start(environment, force_build=False):
                environment.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
                environment._started, environment.transport = True, transport

            async def stop(environment, delete=True):
                environment._started = False
                phases.append("cleanup")

            settings = Settings(cluster=Cluster("https://cluster.example", "ca", "cert", "key"),
                                image="ale-cpu-free", namespace="default", node="", guest_protocol="ale")
            config = TrialConfig.model_validate({
                "trial_name": "ale-native-fixture", "trials_dir": str(root / "trials"),
                "task": {"path": str(task)},
                "environment": {"import_path": "ale_adapter.environment:ALEEnvironment", "kwargs": {
                    "source": str(source), "native_python": PYTHON, "image_map": str(mapping)}},
                "agent": {"import_path": "Agents.AgentsLastExam.agent:ALECommandAgent", "model_name": "fake-model",
                          "kwargs": {"command": "C:\\Agent\\run.cmd", "linux_command": "/opt/agent/run.sh"}},
                "verifier": {"import_path": "ale_adapter.verifier:ALEVerifier"},
            })
            # Emulate a Harbor backend's remote filesystem/processes. The HTTP
            # forwarding helper, proxy, native driver and Harbor trial run unchanged.
            guest_files = {}
            native_spawn = asyncio.create_subprocess_exec
            linux = Mock()
            linux.start = AsyncMock()
            async def cleanup(delete=True):
                if not phases or phases[-1] != "cleanup":
                    phases.append("cleanup")
            linux.stop = AsyncMock(side_effect=cleanup)
            async def upload(source_path, target_path):
                guest_files[str(target_path)] = Path(source_path).read_bytes()
            async def download(source_path, target_path):
                Path(target_path).parent.mkdir(parents=True, exist_ok=True)
                Path(target_path).write_bytes(guest_files[str(source_path)])
            async def execute_linux(command, **kwargs):
                if "/forward.py" in command:
                    args = shlex.split(command)
                    request = json.loads(guest_files[args[-2]])
                    request["url"] = request["url"].replace("http://127.0.0.1:5000", endpoint)
                    with tempfile.TemporaryDirectory() as forward_tmp:
                        forward_root = Path(forward_tmp)
                        (forward_root / "forward.py").write_bytes(guest_files[args[1]])
                        (forward_root / "request.json").write_text(json.dumps(request))
                        process = await native_spawn(PYTHON, str(forward_root / "forward.py"),
                            str(forward_root / "request.json"), str(forward_root / "response"),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        stdout, stderr = await process.communicate()
                        for suffix in (".json", ".body"):
                            if (forward_root / ("response" + suffix)).exists():
                                guest_files[args[-1] + suffix] = (forward_root / ("response" + suffix)).read_bytes()
                        return ExecResult(return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())
                if "bash /logs/agent/run.sh" in command:
                    self.assertEqual(guest_files["/logs/agent/instruction.txt"], b"Write a fractional answer")
                    self.assertEqual(files[f"{guest_root}/setup.txt"], b"ready")
                    self.assertNotIn("reference", phases)
                    phases.append("agent")
                    files[f"{guest_root}/answer.txt"] = b"0.4"
                    guest_files["/logs/agent/stdout.txt"] = b"agent executed"
                return ExecResult(return_code=0, stdout="", stderr="")
            async def download_logs(source_dir=None, target_dir=None, **kwargs):
                if str(source_dir) == "/logs/agent":
                    for path, data in guest_files.items():
                        if path.startswith("/logs/agent/"):
                            target = Path(target_dir) / Path(path).name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(data)
            linux.exec = AsyncMock(side_effect=execute_linux)
            linux.upload_file = AsyncMock(side_effect=upload)
            linux.download_file = AsyncMock(side_effect=download)
            linux.download_dir_filtered = AsyncMock(side_effect=download_logs)
            linux.download_dir = AsyncMock(side_effect=download_logs)

            with patch("kubevirt_windows.environment.Settings.from_env", return_value=settings if windows else None) as cluster, \
                    patch("ale_adapter.environment.sbx_configured", return_value=backend_mode == "sbx"), \
                    patch("ale_adapter.environment.create_backend", return_value=(backend_mode, linux)), \
                    patch.object(KubeVirtWindowsEnvironment, "start", start), \
                    patch.object(KubeVirtWindowsEnvironment, "stop", stop), \
                    patch.dict(os.environ, {"ALE_REFERENCE_ARCHIVE_PASSWORD": "fake-reference-password"}):
                result = await (await Trial.create(config)).run()
            if not windows:
                cluster.assert_not_called()
            trial = root / "trials/ale-native-fixture"
            if fail_setup or cancel_setup:
                self.assertIsNotNone(result.exception_info)
                self.assertIsNone(result.verifier_result)
                self.assertEqual(phases, ["setup", "cleanup"] if cancel_setup else ["cleanup"])
                self.assertTrue((trial / "ale-linux.json").is_file())
                return
            if result.exception_info:
                logs = "\n".join(p.read_text() for p in trial.glob("ale-*.log"))
                self.fail(f"{result.exception_info}\n{logs}")
            self.assertEqual(result.verifier_result.rewards, {"reward": .4})
            self.assertEqual(json.loads((trial / "verifier/native-result.json").read_text())["raw_scores"], [.4, .9])
            self.assertTrue((trial / "result.json").is_file())
            self.assertEqual(phases[:3] if windows else phases[:2], ["resolution", "setup", "agent"] if windows else ["setup", "agent"])
            self.assertLess(phases.index("agent"), phases.index("reference"))
            self.assertLess(phases.index("reference"), phases.index("grade"))
            self.assertEqual(phases[-1], "cleanup")
            if not windows:
                self.assertEqual(json.loads((trial / "ale-linux.json").read_text())["backend"], backend_mode)
                self.assertEqual((trial / "agent/stdout.txt").read_text(), "agent executed")
            shutil.rmtree(source)
