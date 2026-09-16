from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_output import (  # noqa: E402
    OUTPUT_BUDGET_BYTES,
    READ_CHUNK_BYTES,
    _FailureScanner,
    exec_logged_agent,
)


class LocalEnvironment:
    def __init__(self):
        self.responses = []
        self.calls = []

    async def exec(self, command, **kwargs):
        self.calls.append(kwargs)
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=kwargs.get("cwd"), env=kwargs.get("env"), start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), kwargs.get("timeout_sec"),
            )
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
            raise
        self.responses.append(len(stdout) + len(stderr))
        return SimpleNamespace(
            return_code=process.returncode,
            stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"),
        )


class Agent:
    def __init__(self, patterns=()):
        self._compiled_error_patterns = [
            (re.compile(pattern, re.IGNORECASE), RuntimeError) for pattern in patterns
        ]

    async def exec_as_agent(self, environment, command, **kwargs):
        result = await environment.exec(command="set -o pipefail; " + command, **kwargs)
        result.classification = None
        if result.return_code:
            for pattern, _exception in self._compiled_error_patterns:
                if pattern.search(result.stdout + "\n" + result.stderr):
                    result.classification = pattern.pattern
                    break
        return result


class AgentOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_unterminated_stdout_stderr_preserve_files_and_bound_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = LocalEnvironment()
            script = "import os; os.write(1, b'x' * (8*1024*1024)); os.write(2, b'y' * (8*1024*1024))"
            result = await exec_logged_agent(
                Agent(), environment,
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                log_dir=directory,
            )
            self.assertEqual(result.return_code, 0)
            self.assertLessEqual(max(environment.responses), OUTPUT_BUDGET_BYTES)
            for filename, byte in (("command.stdout", b"x"), ("command.stderr", b"y")):
                with (Path(directory) / filename).open("rb") as stream:
                    self.assertEqual(hashlib.file_digest(stream, "sha256").hexdigest(),
                                     hashlib.sha256(byte * (8*1024*1024)).hexdigest())

    async def test_early_error_preserves_priority_after_large_output(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = LocalEnvironment()
            script = "import os; os.write(1, b'API Error\\nrate limit\\n' + b'x'*(2*1024*1024)); raise SystemExit(7)"
            result = await exec_logged_agent(
                Agent([r"rate.?limit", r"API Error"]), environment,
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                log_dir=directory,
            )
            self.assertEqual(result.return_code, 7)
            self.assertEqual(result.classification, r"rate.?limit")
            self.assertLessEqual(max(environment.responses), OUTPUT_BUDGET_BYTES)

    async def test_exit_exec_pipeline_and_finalizer_status(self):
        for command, code in (
            ("printf hi; exit 7", 7), ("exec bash -c 'exit 9'", 9),
            ("false | cat", 1), ("set -e; false; echo unreachable", 1),
            ("false | cat; agent_rc=$?; printf finalized; exit \"$agent_rc\"", 1),
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                result = await exec_logged_agent(Agent(), LocalEnvironment(), command, log_dir=directory)
                self.assertEqual(result.return_code, code)
                self.assertNotIn("unreachable", result.stdout)

    async def test_environment_cwd_and_generic_execution_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = LocalEnvironment()
            result = await exec_logged_agent(
                Agent(), environment, 'printf "%s:%s" "$TEST_VALUE" "$PWD"',
                log_dir=directory, env={"PATH": "/usr/bin:/bin", "TEST_VALUE": "fake-value"},
                cwd=directory, user="test-user", timeout_sec=10,
            )
            self.assertEqual(result.stdout, "fake-value:" + directory)
            self.assertEqual(environment.calls[0]["user"], "test-user")
            generic = await environment.exec("head -c 300000 /dev/zero")
            self.assertEqual(len(generic.stdout), 300000)

    async def test_timeout_propagates_with_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TimeoutError):
                await exec_logged_agent(
                    Agent(), LocalEnvironment(), "printf partial; exec sleep 60",
                    log_dir=directory, timeout_sec=0.1,
                )
            self.assertEqual((Path(directory) / "command.stdout").read_bytes(), b"partial")

    async def test_unknown_classifier_uses_original_capture(self):
        environment = LocalEnvironment()
        with self.assertLogs("agent_output", level="WARNING"):
            result = await exec_logged_agent(
                Agent([r"custom.*error"]), environment, "head -c 300000 /dev/zero",
            )
        self.assertEqual(len(result.stdout), 300000)
        self.assertEqual(len(environment.calls), 1)

    def test_classifier_cross_chunk_unicode_and_long_curl_code(self):
        patterns = Agent([r"rate.?limit", r"curl: \(\d+\)"])._compiled_error_patterns
        scanner = _FailureScanner(patterns)
        for chunk in ("curl: (", "2" * READ_CHUNK_BYTES, "2" * READ_CHUNK_BYTES, ")"):
            scanner.feed(chunk)
            self.assertLessEqual(len(scanner.carry), 256)
        self.assertEqual(scanner.witness, "curl: (0)")
        scanner.feed("rate")
        scanner.feed("限limit")
        self.assertEqual(scanner.best, 0)
        self.assertEqual(scanner.witness, "rate限limit")

    async def test_cancellation_propagates_and_preserves_partial_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            task = asyncio.create_task(exec_logged_agent(
                Agent(), LocalEnvironment(), "printf partial; exec sleep 60", log_dir=directory,
            ))
            path = Path(directory) / "command.stdout"
            for _ in range(100):
                if path.exists() and path.read_bytes() == b"partial":
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(path.read_bytes(), b"partial")
