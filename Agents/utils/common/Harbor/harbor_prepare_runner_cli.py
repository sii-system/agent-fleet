#!/usr/bin/env python3
"""Validate the configured pinned Harbor runner environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO

DEFAULT_REQUIREMENTS_FILE = Path(__file__).with_name("runner-requirements.txt")
VERSION_PROBE = (
    "import importlib.metadata as m, sys; "
    "print(m.version(sys.argv[1]))"
)
PYTHON_VERSION_PROBE = (
    "import sys; "
    "print('.'.join(str(part) for part in sys.version_info[:3]))"
)


def load_requirements(path: Path) -> list[tuple[str, str]]:
    requirements: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        package, separator, version = line.partition("==")
        if not separator or not package.strip() or not version.strip() or "==" in version:
            raise ValueError(f"invalid exact requirement at {path}:{line_number}: {line}")
        requirements.append((package.strip(), version.strip()))
    if not requirements:
        raise ValueError(f"no runner requirements found in {path}")
    return requirements


def validate_runner(log: TextIO) -> bool:
    python = Path(os.environ["HARBOR_OPIK_PYTHON"])
    opik = Path(os.environ["HARBOR_OPIK_BIN"])
    harbor = Path(os.environ["HARBOR_CLI_BIN"])
    requirements = load_requirements(
        Path(os.environ.get("HARBOR_RUNNER_REQUIREMENTS", DEFAULT_REQUIREMENTS_FILE))
    )

    for executable in (python, opik, harbor):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            log.write(f"runner executable is missing: {executable}\n")
            return False

    expected_python = os.environ["HARBOR_RUNNER_PYTHON_VERSION"]
    try:
        result = subprocess.run(
            [str(python), "-c", PYTHON_VERSION_PROBE],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.write(f"failed to query runner Python version: {exc}\n")
        return False
    actual_python = result.stdout.strip() if result.returncode == 0 else None
    if actual_python != expected_python:
        log.write(
            f"runner Python mismatch: expected {expected_python}, "
            f"got {actual_python or 'unknown'}\n"
        )
        return False

    for package, expected in requirements:
        try:
            result = subprocess.run(
                [str(python), "-c", VERSION_PROBE, package],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            log.write(f"failed to query {package}: {exc}\n")
            return False
        actual = result.stdout.strip() if result.returncode == 0 else None
        if actual != expected:
            log.write(
                f"runner package mismatch: {package} expected {expected}, "
                f"got {actual or 'missing'}\n"
            )
            return False

    for command in ([str(opik), "harbor", "run", "--help"], [str(harbor), "run", "--help"]):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            log.write(f"failed to execute {command[0]}: {exc}\n")
            return False
        if result.returncode != 0:
            log.write(result.stdout)
            log.write(result.stderr)
            return False
    return True


def main() -> int:
    runtime_dir = Path(os.environ["RUNTIME_DIR"])
    status_file = Path(os.environ["HARBOR_RUNNER_PREPARE_STATUS_FILE"])
    log_file = Path(os.environ["HARBOR_RUNNER_PREPARE_LOG_FILE"])
    workers_failed_file = Path(os.environ["WORKERS_FAILED_FILE"])

    runtime_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("HARBOR_RUNNER_PREPARE", "1") != "1":
        status_file.write_text("skipped\n", encoding="utf-8")
        return 0

    status_file.write_text("validating\n", encoding="utf-8")
    print("validating configured pinned Harbor runner CLIs...")
    with log_file.open("w", encoding="utf-8") as log:
        if validate_runner(log):
            status_file.write_text("done\n", encoding="utf-8")
            return 0

    status_file.write_text("failed\n", encoding="utf-8")
    workers_failed_file.touch()
    return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate"]:
        raise SystemExit(0 if validate_runner(sys.stderr) else 1)
    if sys.argv[1:]:
        raise SystemExit("usage: harbor_prepare_runner_cli.py [--validate]")
    raise SystemExit(main())
