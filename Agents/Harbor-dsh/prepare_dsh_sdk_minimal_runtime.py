#!/usr/bin/env python3
"""Package the version-matched SDK runtime for the DSH adapter."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_REF = "dsh-v0.1.2-alpha.2"
DEFAULT_SOURCE_SHA = "0a53fb55bea101816fa226bb964ae2bed71c343b"
# Exact transitive set from python/sdk/uv.lock at DEFAULT_SOURCE_SHA.
SDK_RUNTIME_REQUIREMENTS = (
    "annotated-types==0.7.0",
    "pydantic==2.13.4",
    "pydantic-core==2.46.4",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.2",
)


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    source_ref: str
    source_sha: str
    source_dir: Path | None
    runtime_tarball: Path
    version_file: Path
    python_runtime_tarball: Path

    @property
    def source_version(self) -> str:
        return f"{self.source_ref}@{self.source_sha}"

    @property
    def runtime_version(self) -> str:
        dependencies = ",".join(SDK_RUNTIME_REQUIREMENTS)
        return f"{self.source_version};dependencies={dependencies}"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        source_ref = _value(values, "DSH_SDK_MINIMAL_SOURCE_REF", DEFAULT_SOURCE_REF)
        source_sha = _value(values, "DSH_SDK_MINIMAL_SOURCE_SHA", DEFAULT_SOURCE_SHA)
        source_dir_value = values.get("DSH_SDK_MINIMAL_SOURCE_DIR", "").strip()
        runtime_basename = _value(
            values,
            "DSH_SDK_MINIMAL_RUNTIME_BASENAME",
            f"dsh-sdk-minimal-runtime-{source_ref}.tar.gz",
        )
        return cls(
            wheel_dir=wheel_dir,
            source_ref=source_ref,
            source_sha=source_sha,
            source_dir=Path(source_dir_value) if source_dir_value else None,
            runtime_tarball=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_RUNTIME_TARBALL",
                    str(wheel_dir / runtime_basename),
                )
            ),
            version_file=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_RUNTIME_VERSION_FILE",
                    str(wheel_dir / "dsh-sdk-minimal-runtime.version"),
                )
            ),
            python_runtime_tarball=Path(
                _value(
                    values,
                    "DSH_SDK_MINIMAL_PYTHON_RUNTIME_TARBALL",
                    str(wheel_dir / "dsh-sdk-minimal-python3.12-runtime.tar.gz"),
                )
            ),
        )


def tarball_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with tarfile.open(path) as archive:
            archive.getmembers()
    except (OSError, tarfile.TarError):
        return False
    return True


def managed_python_root() -> Path:
    """Return the uv-managed Python root used for the portable runtime."""
    python_real = Path(sys.executable).resolve()
    python_root = python_real.parents[1]
    if (
        sys.version_info[:2] != (3, 12)
        or not (python_root / "bin").is_dir()
        or not (python_root / "BUILD").is_file()
    ):
        raise RuntimeError(
            "runtime preparation requires a managed Python 3.12 "
            "python-build-standalone installation"
        )
    return python_root


def prepare_python_runtime(config: Config) -> None:
    if tarball_ready(config.python_runtime_tarball):
        print(f"[prepare] reuse Python runtime: {config.python_runtime_tarball}")
        return

    python_root = managed_python_root()

    config.python_runtime_tarball.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config.python_runtime_tarball.name}.",
        suffix=".tmp",
        dir=config.python_runtime_tarball.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(python_root, arcname="dsh-sdk-minimal-python3.12-runtime")
        if not tarball_ready(temporary):
            raise RuntimeError("generated Python runtime archive is invalid")
        os.replace(temporary, config.python_runtime_tarball)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"[prepare] built Python runtime: {config.python_runtime_tarball}")


def runtime_ready(config: Config) -> bool:
    if not tarball_ready(config.runtime_tarball) or not tarball_ready(
        config.python_runtime_tarball
    ):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.runtime_version


def verified_sdk_source(config: Config) -> Path:
    """Validate and return a clean, version-matched local SDK checkout."""
    assert config.source_dir is not None
    completed = subprocess.run(
        ["git", "-C", str(config.source_dir), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    )
    actual_sha = completed.stdout.strip()
    if actual_sha != config.source_sha:
        raise RuntimeError(
            "DSH SDK source checkout mismatch: "
            f"expected {config.source_sha}, got {actual_sha}"
        )
    sdk_dir = config.source_dir / "python" / "sdk"
    if not (sdk_dir / "pyproject.toml").is_file():
        raise RuntimeError(f"DSH SDK source is missing: {sdk_dir}")
    status = subprocess.run(
        [
            "git",
            "-C",
            str(config.source_dir),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "python/sdk",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    if status.stdout.strip():
        raise RuntimeError("DSH SDK source checkout has local changes under python/sdk")
    return sdk_dir


def prepare(config: Config) -> None:
    prepare_python_runtime(config)
    if runtime_ready(config):
        print(
            f"[prepare] skip DSH sdk-minimal runtime (cached): {config.runtime_tarball}"
        )
        return

    config.wheel_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-fleet-dsh-sdk-minimal-", dir="/tmp"
    ) as temporary_name:
        temporary = Path(temporary_name)
        runtime_root = temporary / "dsh-sdk-minimal-runtime"
        site_packages = runtime_root / "site-packages"
        site_packages.mkdir(parents=True)

        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--target",
                str(site_packages),
                *SDK_RUNTIME_REQUIREMENTS,
            ],
            check=True,
        )
        if config.source_dir is None:
            source_requirement = (
                "deepseek-harness-sdk @ "
                "https://github.com/deepseek-ai/deepseek-harness/archive/"
                f"{config.source_sha}.tar.gz#subdirectory=python/sdk"
            )
        else:
            source_requirement = str(verified_sdk_source(config))
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(site_packages),
                source_requirement,
            ],
            check=True,
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from deepseek_harness import DeepSeekHarnessConfig; "
                    "fields=DeepSeekHarnessConfig.__dataclass_fields__; "
                    "required={'profile','dsh_home','dsh_bin','reasoning_effort'}; "
                    "missing=required-fields.keys(); "
                    "assert not missing, sorted(missing); print('sdk-minimal-api-ok')"
                ),
            ],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(site_packages)},
        )
        if completed.stdout.strip() != "sdk-minimal-api-ok":
            raise RuntimeError("prepared SDK failed its profile API conformance check")
        (runtime_root / "SOURCE_VERSION").write_text(
            f"{config.runtime_version}\n", encoding="utf-8"
        )

        file_descriptor, temporary_tar_name = tempfile.mkstemp(
            prefix=f".{config.runtime_tarball.name}.",
            suffix=".tmp",
            dir=config.runtime_tarball.parent,
        )
        os.close(file_descriptor)
        temporary_tar = Path(temporary_tar_name)
        try:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(runtime_root, arcname=runtime_root.name)
            if not tarball_ready(temporary_tar):
                raise RuntimeError("generated DSH sdk-minimal archive is invalid")
            os.replace(temporary_tar, config.runtime_tarball)
        finally:
            temporary_tar.unlink(missing_ok=True)

    temporary_version = config.version_file.with_suffix(".version.tmp")
    temporary_version.write_text(f"{config.runtime_version}\n", encoding="utf-8")
    os.replace(temporary_version, config.version_file)
    print(f"[prepare] built DSH sdk-minimal runtime: {config.runtime_tarball}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-runtime-version", action="store_true")
    args = parser.parse_args(argv)
    config = Config.from_environment()
    if args.print_runtime_version:
        print(config.runtime_version)
        return 0
    prepare(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
