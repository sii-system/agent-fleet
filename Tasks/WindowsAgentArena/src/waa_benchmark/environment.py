"""Harbor environment attaching native WAA phases to an owned Windows VM."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from kubevirt_windows.environment import KubeVirtWindowsEnvironment

from .dataset import BENCHMARKS, digest
from .source import validate_runtime


class WAASession:
    def __init__(self, *args, runtime, pcagent_runtime, screen_size=None, action_space="pyautogui", a11y=False,
                 native_python=None, **kwargs):
        self.runtime, self.pcagent_runtime = Path(runtime).resolve(), Path(pcagent_runtime).resolve()
        self.screen_size, self.action_space, self.a11y = screen_size, action_space, a11y
        self.native_python = native_python or sys.executable
        self.worker, self.worker_log = None, None
        self._worker_spec_path = None
        self._setup_complete = False
        self._evaluate_attempted = False
        self._phase_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)
        self.native = json.loads((self.environment_dir / "waa.json").read_text())
        self.benchmark = self.native["benchmark"]
        release = BENCHMARKS[self.benchmark]
        if self.native["revision"] != release["revision"]:
            raise ValueError("Harbor task uses a different WAA revision")
        validate_runtime(self.runtime, benchmark=self.benchmark)
        validate_runtime(self.pcagent_runtime)
        if digest(self.environment_dir / "native-task.json") != self.native["task_sha256"]:
            raise ValueError("Native WAA task checksum mismatch")
        if self.settings.guest_protocol != "waa" or self.settings.guest_port != 5000:
            raise ValueError("WAA tasks require the WAA command server on guest port 5000")
        self._validate_native_ports()
        if action_space not in ("pyautogui", "code_block", "computer_13"):
            raise ValueError("Unsupported native WAA action space")
        self.screen_size = tuple(screen_size or release["screen_size"])
        if len(self.screen_size) != 2 or any(int(value) <= 0 for value in self.screen_size):
            raise ValueError("Screenshot dimensions must be positive")

    async def start(self, force_build=False):
        if self.worker is not None and self.worker.returncode is None:
            return
        if self._started:
            raise RuntimeError("A WAA trial VM cannot restart its native session")
        try:
            await super().start(force_build=force_build)
            endpoints = await self.control.guest_endpoints(self.vm_name, owner=self.token[:12])
            native_dir = self.trial_paths.agent_dir / "native"
            native_dir.mkdir(parents=True, exist_ok=True)
            spec_path = self.trial_paths.trial_dir / "waa-worker.json"
            spec_path.write_text(json.dumps({
                "benchmark": self.benchmark, "runtime": str(self.runtime),
                "pcagent_client": str(self.pcagent_runtime / "client"),
                "task_path": str(self.environment_dir / "native-task.json"),
                "task_sha256": self.native["task_sha256"], "domain": self.native["domain"],
                "host": "waa-guest.invalid", "endpoints": endpoints, "output": str(native_dir),
                "screen_size": self.screen_size, "action_space": self.action_space, "a11y": self.a11y,
            }, indent=2) + "\n")
            self._worker_spec_path = spec_path
            self._setup_complete = False
            self._evaluate_attempted = False
            await self._start_worker(spec_path, append_log=False)
            await self.native_request("setup")
            self._setup_complete = True
        except BaseException:
            await asyncio.shield(self.stop(delete=True))
            raise

    async def _start_worker(self, spec_path: Path, *, append_log: bool = False):
        mode = "ab" if append_log else "wb"
        log_file = (self.trial_paths.trial_dir / "waa-worker.log").open(mode)
        try:
            worker = await asyncio.create_subprocess_exec(
                self.native_python, "-m", "waa_benchmark.worker", str(spec_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=log_file, start_new_session=True,
            )
        except BaseException:
            log_file.close()
            raise
        self.worker_log = log_file
        self.worker = worker

    async def native_request(self, method, **kwargs):
        async with self._phase_lock:
            reattach = False
            if self.worker is None or self.worker.returncode is not None:
                spec_path = getattr(self, "_worker_spec_path", None)
                can_reattach = (
                    method == "evaluate"
                    and getattr(self, "_setup_complete", False)
                    and getattr(self, "_started", False)
                    and not getattr(self, "_evaluate_attempted", False)
                    and spec_path is not None
                    and Path(spec_path).is_file()
                )
                if not can_reattach:
                    raise RuntimeError("Native WAA worker is not running")
                self._evaluate_attempted = True
                if self.worker is not None:
                    await self._stop_worker()
                await self._start_worker(Path(spec_path), append_log=True)
                reattach = True
            elif method == "evaluate":
                self._evaluate_attempted = True
            payload = {"method": method, **kwargs}
            if reattach:
                payload["reattach"] = True
            try:
                self.worker.stdin.write((json.dumps(payload) + "\n").encode())
                await self.worker.stdin.drain()
                line = await self.worker.stdout.readline()
                if not line:
                    raise RuntimeError("Native WAA worker exited; see waa-worker.log")
                response = json.loads(line)
            except BaseException:
                # A cancelled or broken phase must not continue changing the
                # desktop while Harbor starts verification or cleanup.
                await asyncio.shield(self._stop_worker())
                raise
            if not response["ok"]:
                if method != "agent":
                    await asyncio.shield(self._stop_worker())
                raise RuntimeError(f"Native WAA {method} failed ({response['error_type']}); see waa-worker.log")
            return response["result"]

    async def _stop_worker(self):
        worker, self.worker = self.worker, None
        try:
            if worker is not None:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await worker.wait()
        finally:
            if self.worker_log is not None:
                self.worker_log.close()
                self.worker_log = None

    async def stop(self, delete=True):
        self._setup_complete = False
        try:
            await self._stop_worker()
        finally:
            await super().stop(delete=delete)


class WAAEnvironment(WAASession, KubeVirtWindowsEnvironment):
    @staticmethod
    def type():
        return "waa-kubevirt-windows"

    def _validate_native_ports(self):
        if self.settings.guest_node_port or not {9222, 8080}.issubset(self.settings.extra_guest_ports):
            raise ValueError("WAA requires dynamic NodePorts and auxiliary guest ports 9222,8080")
