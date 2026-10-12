from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

import qz_create_limiter as limiter  # noqa: E402
from qz_create_limiter import (  # noqa: E402
    qz_create_concurrency,
    qz_create_lock_dir,
    qz_create_slot,
)


class QzCreateLimiterTest(unittest.TestCase):
    def test_concurrency_defaults_to_ten_and_accepts_operator_override(self):
        self.assertEqual(qz_create_concurrency({}), 10)
        self.assertEqual(qz_create_concurrency({"QZ_CREATE_CONCURRENCY": "40"}), 40)

    def test_concurrency_rejects_invalid_values(self):
        for value in ("nope", "0", "-1", "1.5"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "positive integer",
            ):
                qz_create_concurrency({"QZ_CREATE_CONCURRENCY": value})

    def test_lock_directory_is_shared_across_runs_on_one_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = qz_create_lock_dir(
                "https://qz.example/v1",
                {
                    "XDG_RUNTIME_DIR": temporary,
                    "OUTPUT_PATH": "/runs/first",
                    "RUNTIME_DIR": "/runs/first/runtime",
                },
            )
            second = qz_create_lock_dir(
                "https://qz.example/v1/",
                {
                    "XDG_RUNTIME_DIR": temporary,
                    "OUTPUT_PATH": "/runs/second",
                    "RUNTIME_DIR": "/runs/second/runtime",
                },
            )

        self.assertEqual(first, second)

    def test_rolling_window_caps_async_create_calls(self):
        async def exercise(runtime_dir: str) -> int:
            active = 0
            maximum = 0
            environ = {
                "XDG_RUNTIME_DIR": runtime_dir,
                "QZ_CREATE_CONCURRENCY": "3",
            }

            async def create_one() -> None:
                nonlocal active, maximum
                async with qz_create_slot("https://qz.example/v1", environ):
                    active += 1
                    maximum = max(maximum, active)
                    await asyncio.sleep(0.03)
                    active -= 1

            await asyncio.gather(*(create_one() for _ in range(12)))
            return maximum

        with tempfile.TemporaryDirectory() as temporary:
            maximum = asyncio.run(exercise(temporary))

        self.assertEqual(maximum, 3)

    def test_slot_is_released_when_create_raises(self):
        async def exercise(runtime_dir: str) -> None:
            environ = {
                "XDG_RUNTIME_DIR": runtime_dir,
                "QZ_CREATE_CONCURRENCY": "1",
            }
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                async with qz_create_slot("https://qz.example/v1", environ):
                    raise RuntimeError("create failed")

            async def acquire_again() -> None:
                async with qz_create_slot("https://qz.example/v1", environ):
                    pass

            await asyncio.wait_for(acquire_again(), timeout=1)

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(temporary))

    def test_contention_uses_jittered_exponential_backoff(self):
        class StopPolling(Exception):
            pass

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 5:
                raise StopPolling

        async def exercise(runtime_dir: str) -> None:
            environ = {
                "XDG_RUNTIME_DIR": runtime_dir,
                "QZ_CREATE_CONCURRENCY": "1",
            }
            async with qz_create_slot("https://qz.example/v1", environ):
                self.fail("slot unexpectedly acquired")

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(limiter, "_try_lock_slot", return_value=None),
            patch.object(
                limiter.random,
                "uniform",
                side_effect=lambda lower, upper: (lower + upper) / 2,
            ),
            patch.object(limiter.asyncio, "sleep", new=fake_sleep),
            self.assertRaises(StopPolling),
        ):
            asyncio.run(exercise(temporary))

        self.assertEqual(delays, [0.02, 0.04, 0.08, 0.16, 0.25])

    def test_slot_is_shared_across_worker_processes(self):
        worker = textwrap.dedent(
            """
            import asyncio
            import json
            import os
            import sys
            import time

            from qz_create_limiter import qz_create_slot

            async def main():
                started = time.monotonic()
                async with qz_create_slot("https://qz.example/v1", os.environ):
                    acquired = time.monotonic()
                    print(json.dumps({"wait_sec": acquired - started}), flush=True)
                    await asyncio.sleep(float(sys.argv[1]))

            asyncio.run(main())
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONPATH": str(HARBOR_DIR),
                    "XDG_RUNTIME_DIR": temporary,
                    "QZ_CREATE_CONCURRENCY": "1",
                }
            )
            first = subprocess.Popen(
                [sys.executable, "-c", worker, "0.4"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = None
            try:
                assert first.stdout is not None
                first_record = json.loads(first.stdout.readline())
                second = subprocess.Popen(
                    [sys.executable, "-c", worker, "0"],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                second_stdout, second_stderr = second.communicate(timeout=3)
                first_stdout, first_stderr = first.communicate(timeout=3)
            finally:
                for process in (first, second):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second_stderr)
        self.assertEqual(first_stdout, "")
        second_record = json.loads(second_stdout)
        self.assertLess(first_record["wait_sec"], 0.2)
        self.assertGreater(second_record["wait_sec"], 0.25)


if __name__ == "__main__":
    unittest.main()
