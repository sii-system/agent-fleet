import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / ".github" / "scripts" / "e2e_worker_capacity.py"

spec = importlib.util.spec_from_file_location("e2e_worker_capacity", MODULE_PATH)
capacity_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity_module)


class ReadAvailableGbTest(unittest.TestCase):
    def test_reads_mem_available_in_gibibytes(self):
        meminfo = "\n".join(
            [
                "MemTotal:       1610612736 kB",
                "MemFree:         104857600 kB",
                "MemAvailable:   1572864000 kB",
            ]
        )
        self.assertEqual(capacity_module.read_available_gb(meminfo), 1500)

    def test_rejects_meminfo_without_mem_available(self):
        with self.assertRaises(ValueError):
            capacity_module.read_available_gb("MemTotal: 123 kB")


class CapacityTest(unittest.TestCase):
    def test_cpu_binds_on_the_current_bare_hosts(self):
        # 128 cores, 1.5 TB: (128-16)//8 = 14 vs (1536-64)//32 = 46
        self.assertEqual(capacity_module.capacity(128, 1536), 14)

    def test_memory_can_bind_instead(self):
        # 128 cores but only 192 GB available: 14 vs (192-64)//32 = 4
        self.assertEqual(capacity_module.capacity(128, 192), 4)

    def test_reserves_headroom_for_colocated_container_runners(self):
        # Without the 16-core reserve this would be 128//8 = 16
        self.assertLess(capacity_module.capacity(128, 1536), 16)

    def test_small_host_has_no_capacity(self):
        self.assertEqual(capacity_module.capacity(16, 64), 0)


class ResolveTest(unittest.TestCase):
    def test_uses_full_capacity_when_nothing_requested(self):
        workers, note = capacity_module.resolve(128, 1536, None)
        self.assertEqual(workers, 14)
        self.assertIn("14", note)

    def test_reduces_a_request_above_capacity_and_says_so(self):
        workers, note = capacity_module.resolve(128, 1536, 40)
        self.assertEqual(workers, 14)
        self.assertIn("reduced", note.lower())

    def test_honours_a_request_below_capacity(self):
        workers, note = capacity_module.resolve(128, 1536, 4)
        self.assertEqual(workers, 4)
        self.assertNotIn("reduced", note.lower())

    def test_rejects_a_request_below_one(self):
        with self.assertRaises(capacity_module.CapacityError):
            capacity_module.resolve(128, 1536, 0)

    def test_fails_fast_when_the_host_cannot_fit_one_worker(self):
        with self.assertRaises(capacity_module.CapacityError):
            capacity_module.resolve(16, 64, None)


class ParseRequestedTest(unittest.TestCase):
    def test_empty_means_derive_from_capacity(self):
        self.assertIsNone(capacity_module.parse_requested("  "))

    def test_reads_a_positive_integer(self):
        self.assertEqual(capacity_module.parse_requested(" 6 "), 6)

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            capacity_module.parse_requested("abc")

    def test_rejects_a_float(self):
        with self.assertRaises(ValueError):
            capacity_module.parse_requested("4.5")


class MainTest(unittest.TestCase):
    def _meminfo(self, tmp, text="MemAvailable:   1572864000 kB\n"):
        path = Path(tmp) / "meminfo"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_writes_the_worker_count_to_github_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "gh-output"
            with mock.patch("os.cpu_count", return_value=128), mock.patch.dict(
                os.environ, {"GITHUB_OUTPUT": str(output)}
            ):
                code = capacity_module.main(["--meminfo", self._meminfo(tmp)])
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "workers=14\n")

    def test_malformed_requested_value_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = capacity_module.main(
                    ["--requested", "abc", "--meminfo", self._meminfo(tmp)]
                )
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())

    def test_missing_meminfo_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = capacity_module.main(
                    ["--meminfo", str(Path(tmp) / "absent")]
                )
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())

    def test_meminfo_without_mem_available_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = capacity_module.main(
                    ["--meminfo", self._meminfo(tmp, "MemTotal: 123 kB\n")]
                )
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())

    def test_a_host_too_small_for_one_worker_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with mock.patch("os.cpu_count", return_value=4), contextlib.redirect_stderr(
                stderr
            ):
                code = capacity_module.main(["--meminfo", self._meminfo(tmp)])
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
