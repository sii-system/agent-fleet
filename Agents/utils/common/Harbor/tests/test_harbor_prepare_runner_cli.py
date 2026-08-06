from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HARBOR_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "harbor_prepare_runner_cli", HARBOR_DIR / "harbor_prepare_runner_cli.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RunnerValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.python = self.root / "bin/python"
        self.opik = self.root / "bin/opik"
        self.harbor = self.root / "bin/harbor"
        self.runtime = self.root / "runtime"
        self.requirements = HARBOR_DIR / "runner-requirements.txt"
        self._write_python("0.18.0")
        for executable in (self.opik, self.harbor):
            self._write_executable(executable, "#!/bin/sh\nexit 0\n")
        self.environment = {
            "HARBOR_OPIK_PYTHON": str(self.python),
            "HARBOR_OPIK_BIN": str(self.opik),
            "HARBOR_CLI_BIN": str(self.harbor),
            "HARBOR_RUNNER_PYTHON_VERSION": "3.12.13",
            "HARBOR_RUNNER_REQUIREMENTS": str(self.requirements),
            "RUNTIME_DIR": str(self.runtime),
            "HARBOR_RUNNER_PREPARE_STATUS_FILE": str(self.runtime / "status"),
            "HARBOR_RUNNER_PREPARE_LOG_FILE": str(self.runtime / "runner.log"),
            "WORKERS_FAILED_FILE": str(self.runtime / "workers.failed"),
            "HARBOR_RUNNER_PREPARE": "1",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_python(
        self, harbor_version: str, python_version: str = "3.12.13"
    ) -> None:
        self._write_executable(
            self.python,
            "#!/bin/sh\n"
            f"[ \"$#\" = 2 ] && echo {python_version} && exit 0\n"
            f"[ \"$3\" = harbor ] && echo {harbor_version} && exit 0\n"
            "[ \"$3\" = e2b ] && echo 2.32.1 && exit 0\n"
            "[ \"$3\" = opik ] && echo 2.1.32 && exit 0\n"
            "[ \"$3\" = pip ] && echo 25.2 && exit 0\n"
            "[ \"$3\" = s3cmd ] && echo 2.4.0 && exit 0\n"
            "[ \"$3\" = yicloud-sdk-python ] && echo 0.3.1 && exit 0\n"
            "exit 1\n",
        )

    def test_manifest_contains_only_exact_production_versions(self) -> None:
        self.assertEqual(
            MODULE.load_requirements(self.requirements),
            [
                ("harbor", "0.18.0"),
                ("e2b", "2.32.1"),
                ("opik", "2.1.32"),
                ("pip", "25.2"),
                ("s3cmd", "2.4.0"),
                ("yicloud-sdk-python", "0.3.1"),
            ],
        )
        invalid = self.root / "invalid.txt"
        invalid.write_text("harbor>=0.18.0\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid exact requirement"):
            MODULE.load_requirements(invalid)

    def test_validation_checks_versions_and_both_clis(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            self.assertTrue(MODULE.validate_runner(io.StringIO()))

        self._write_python("0.20.0")
        log = io.StringIO()
        with mock.patch.dict(os.environ, self.environment):
            self.assertFalse(MODULE.validate_runner(log))
        self.assertIn("expected 0.18.0, got 0.20.0", log.getvalue())

        self._write_python("0.18.0", "3.11.9")
        log = io.StringIO()
        with mock.patch.dict(os.environ, self.environment):
            self.assertFalse(MODULE.validate_runner(log))
        self.assertIn("expected 3.12.13, got 3.11.9", log.getvalue())

    def test_main_writes_done_or_failed_status(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(MODULE.main(), 0)
        self.assertEqual((self.runtime / "status").read_text(), "done\n")

        self.harbor.unlink()
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(MODULE.main(), 1)
        self.assertEqual((self.runtime / "status").read_text(), "failed\n")
        self.assertTrue((self.runtime / "workers.failed").is_file())


if __name__ == "__main__":
    unittest.main()
