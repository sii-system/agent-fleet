#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    script_dir: Path
    wheel_dir: Path
    python_bin: str
    claude_code_version: str
    opencode_version: str
    prepare_opencode_cache: bool
    npm_registry_url: str
    claude_code_npm_spec: str
    claude_code_tgz_basename: str
    opencode_tgz_basename: str
    opencode_linux_x64_tgz_basename: str
    py312_runtime_tarball: Path
    node_runtime_tarball: Path
    claude_npm_cache_dir: Path
    cache_schema: str

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        script_dir: Path | None = None,
    ) -> Config:
        values = os.environ if environ is None else environ
        root = Path(__file__).resolve().parent if script_dir is None else script_dir
        wheel_dir = Path(_value(values, "WHEEL_DIR", str(root / "python-wheels")))
        claude_version = _value(values, "CLAUDE_CODE_VERSION", "latest")
        opencode_version = _value(values, "OPENCODE_VERSION", "latest")
        return cls(
            script_dir=root,
            wheel_dir=wheel_dir,
            python_bin=_value(values, "PYTHON_BIN", "python3.12"),
            claude_code_version=claude_version,
            opencode_version=opencode_version,
            prepare_opencode_cache=values.get("PREPARE_OPENCODE_CACHE", "0") == "1",
            npm_registry_url=_value(
                values,
                "NPM_REGISTRY_URL",
                _value(values, "NPM_CONFIG_REGISTRY", "https://registry.npmjs.org"),
            ),
            claude_code_npm_spec=f"@anthropic-ai/claude-code@{claude_version}",
            claude_code_tgz_basename=_value(
                values,
                "CLAUDE_CODE_TGZ_BASENAME",
                f"claude-code-{claude_version}.tgz",
            ),
            opencode_tgz_basename=_value(
                values,
                "OPENCODE_TGZ_BASENAME",
                f"opencode-ai-{opencode_version}.tgz",
            ),
            opencode_linux_x64_tgz_basename=_value(
                values,
                "OPENCODE_LINUX_X64_TGZ_BASENAME",
                f"opencode-linux-x64-{opencode_version}.tgz",
            ),
            py312_runtime_tarball=Path(
                _value(
                    values,
                    "PY312_RUNTIME_TARBALL",
                    str(wheel_dir / "python3.12-runtime.tar.gz"),
                )
            ),
            node_runtime_tarball=Path(
                _value(
                    values,
                    "NODE_RUNTIME_TARBALL",
                    str(wheel_dir / "node-runtime.tar.xz"),
                )
            ),
            claude_npm_cache_dir=Path(
                _value(
                    values,
                    "CLAUDE_NPM_CACHE_DIR",
                    str(wheel_dir / "npm-cache"),
                )
            ),
            cache_schema=_value(values, "CACHE_SCHEMA", "3"),
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


def npm_tarball_urls(original_url: str, registry_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(original_url)
    registry_origin = registry_url.rstrip("/")
    if (
        parsed.netloc == "registry.npmjs.org"
        and "registry.npmjs.org" not in registry_origin
    ):
        return [registry_origin + parsed.path, original_url]
    return [original_url]


def _manifest_packages(config: Config) -> str:
    packages = (
        "opik,uuid6,socksio,pip,setuptools,wheel,get-pip.py,"
        "python3.12-runtime.tar.gz,node-runtime.tar.xz,npm-cache,"
        f"npm-cache-ready,@anthropic-ai/claude-code@{config.claude_code_version}"
    )
    if config.prepare_opencode_cache:
        packages += (
            f",opencode-ai@{config.opencode_version},"
            f"opencode-linux-x64@{config.opencode_version}"
        )
    return packages


def render_manifest(config: Config, generated_at: datetime | None = None) -> str:
    timestamp = generated_at or datetime.now(timezone.utc)
    utc_timestamp = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    enabled = "1" if config.prepare_opencode_cache else "0"
    return (
        f"generated_at={utc_timestamp}\n"
        f"cache_schema={config.cache_schema}\n"
        f"python_bin={config.python_bin}\n"
        f"claude_code_version={config.claude_code_version}\n"
        f"opencode_version={config.opencode_version}\n"
        f"prepare_opencode_cache={enabled}\n"
        f"claude_npm_cache_version={config.claude_code_version}\n"
        "local_deps_minimal=false\n"
        f"packages={_manifest_packages(config)}\n"
    )


def _read_json(url: str) -> Any:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


def _download(url: str, destination: Path, *, timeout: int | None = None) -> None:
    with (
        urllib.request.urlopen(url, timeout=timeout) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _download_atomic(
    urls: list[str],
    target: Path,
    *,
    prefix: str,
    suffix: str,
    validate: Callable[[Path], bool],
    label: str,
    timeout: int | None = None,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=target.parent
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            downloaded_url = ""
            for candidate in urls:
                try:
                    _download(candidate, temporary, timeout=timeout)
                    downloaded_url = candidate
                    break
                except OSError as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
                    temporary.touch()
            if not downloaded_url:
                assert last_error is not None
                raise last_error
            if not validate(temporary):
                raise ValueError(f"downloaded archive is invalid: {downloaded_url}")
            os.replace(temporary, target)
            return downloaded_url
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            print(
                f"download {label} attempt {attempt}/3 failed: {exc}; retrying",
                flush=True,
            )
            time.sleep(2 * attempt)
    assert last_error is not None
    raise last_error


class DependencyPreparer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._temporary_paths: list[Path] = []

    def run(self) -> None:
        self.config.wheel_dir.mkdir(parents=True, exist_ok=True)
        print(f"[prepare] target dir: {self.config.wheel_dir}")
        try:
            self._prepare_all()
        finally:
            for path in self._temporary_paths:
                shutil.rmtree(path, ignore_errors=True)

    def _prepare_all(self) -> None:
        self._ensure_clean_opik_cache()
        for package, pattern in (
            ("opik", "opik-*.whl"),
            ("uuid6", "uuid6-*.whl"),
            ("socksio", "socksio-*.whl"),
            ("pip", "pip-*.whl"),
            ("setuptools", "setuptools-*.whl"),
            ("wheel", "wheel-*.whl"),
        ):
            self._download_python_package(package, pattern)
        self._download_py313_hook_wheels()
        if self._count_wheels("opik-*.whl") != 1:
            print("expected exactly one opik wheel after prepare", file=sys.stderr)
            for path in self.config.wheel_dir.glob("opik-*.whl"):
                print(path, file=sys.stderr)
            raise RuntimeError("invalid Opik wheel cache")

        self._build_py312_runtime_tarball()
        self._prepare_node_runtime_tarball()
        self._prepare_get_pip()

        claude_meta_url = self._metadata_url(
            "@anthropic-ai/claude-code", self.config.claude_code_version
        )
        self._pack_npm_to_cache(
            self.config.claude_code_npm_spec,
            self.config.claude_code_tgz_basename,
            claude_meta_url,
            "anthropic-ai-claude-code-*.tgz",
        )
        self._prepare_claude_npm_cache()
        (self.config.wheel_dir / "npm-cache-ready").write_text(
            f"{self.config.claude_code_version}\n", encoding="utf-8"
        )

        opencode_meta_url = self._metadata_url(
            "opencode-ai", self.config.opencode_version
        )
        if self.config.prepare_opencode_cache:
            self._download_npm_tgz_to_cache(
                self.config.opencode_tgz_basename, opencode_meta_url
            )
            metadata = _read_json(opencode_meta_url)
            version = str(metadata["version"])
            platform_version = (
                "latest"
                if self.config.opencode_version == "latest"
                else version
            )
            platform_url = self._metadata_url(
                "opencode-linux-x64", platform_version
            )
            self._download_npm_tgz_to_cache(
                self.config.opencode_linux_x64_tgz_basename, platform_url
            )
        else:
            print("[prepare] skip OpenCode npm cache (PREPARE_OPENCODE_CACHE=0)")

        (self.config.wheel_dir / "manifest.txt").write_text(
            render_manifest(self.config), encoding="utf-8"
        )
        print("[prepare] done")

    def _manifest_has(self, line: str) -> bool:
        path = self.config.wheel_dir / "manifest.txt"
        return path.is_file() and line in path.read_text(encoding="utf-8").splitlines()

    def _count_wheels(self, pattern: str) -> int:
        return sum(path.is_file() for path in self.config.wheel_dir.glob(pattern))

    def _ensure_clean_opik_cache(self) -> None:
        if not self._manifest_has(f"cache_schema={self.config.cache_schema}") or (
            self._count_wheels("opik-*.whl") != 1
        ):
            for path in self.config.wheel_dir.glob("opik-*.whl"):
                path.unlink()

    def _download_python_package(self, package: str, pattern: str) -> None:
        if self._count_wheels(pattern):
            print(f"[prepare] skip {package} (cached)")
            return
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--dest",
                str(self.config.wheel_dir),
                package,
            ],
            check=True,
        )

    def _download_py313_hook_wheels(self) -> None:
        patterns = (
            "rapidfuzz-*-cp313-*.whl",
            "watchfiles-*-cp313-*.whl",
            "pydantic_core-*-cp313-*.whl",
        )
        if all(self._count_wheels(pattern) for pattern in patterns):
            print("[prepare] skip Python 3.13 hook wheels (cached)")
            return
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--dest",
                str(self.config.wheel_dir),
                "--only-binary=:all:",
                "--platform",
                "manylinux2014_x86_64",
                "--python-version",
                "3.13",
                "--implementation",
                "cp",
                "--abi",
                "cp313",
                "opik",
                "uuid6",
                "socksio",
            ],
            check=True,
        )

    def _build_py312_runtime_tarball(self) -> None:
        target = self.config.py312_runtime_tarball
        if target.is_file():
            print("[prepare] skip python3.12 runtime tarball (cached)")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            runtime_root = temporary / "python3.12-runtime"
            runtime_bin = runtime_root / "bin"
            runtime_lib = runtime_root / "lib"
            runtime_bin.mkdir(parents=True)
            runtime_lib.mkdir()

            python_real = Path(sys.executable).resolve()
            stdlib = Path(sysconfig.get_path("stdlib"))
            version = sysconfig.get_config_var("VERSION") or "3.12"
            libdir = Path(sysconfig.get_config_var("LIBDIR") or "")
            libpython = next(iter(sorted(libdir.glob(f"libpython{version}*.so*"))), None)
            shutil.copy2(python_real, runtime_bin / "python3.12.real")
            shutil.copytree(stdlib, runtime_lib / "python3.12", symlinks=True)
            if libpython and libpython.is_file():
                shutil.copy2(libpython, runtime_lib / libpython.name, follow_symlinks=False)

            system_lib = runtime_lib / "system"
            system_lib.mkdir()
            completed = subprocess.run(
                ["ldd", str(runtime_bin / "python3.12.real")],
                check=True,
                text=True,
                capture_output=True,
            )
            libraries = {
                Path(field)
                for field in completed.stdout.split()
                if field.startswith("/") and Path(field).is_file()
            }
            for library in libraries:
                try:
                    shutil.copy2(library, system_lib / library.name, follow_symlinks=False)
                except OSError:
                    pass

            wrapper = runtime_bin / "python3.12"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
                'RUNTIME_ROOT="$(cd "${SELF_DIR}/.." && pwd)"\n'
                'export PYTHONHOME="${RUNTIME_ROOT}"\n'
                'export LD_LIBRARY_PATH="${RUNTIME_ROOT}/lib/system:'
                '${RUNTIME_ROOT}/lib:${LD_LIBRARY_PATH:-}"\n'
                'exec "${SELF_DIR}/python3.12.real" "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            with tarfile.open(target, "w:gz") as archive:
                archive.add(runtime_root, arcname=runtime_root.name)
        print(f"[prepare] built python3.12 runtime tarball: {target}")

    def _prepare_node_runtime_tarball(self) -> None:
        target = self.config.node_runtime_tarball
        if tarball_ready(target):
            print("[prepare] skip node runtime tarball (cached)")
            return
        target.unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        index = _read_json("https://nodejs.org/dist/index.json")
        if not isinstance(index, list):
            raise TypeError("expected Node.js release index to be a list")
        version = next(
            str(item["version"])
            for item in index
            if isinstance(item, dict) and str(item.get("version", "")).startswith("v22.")
        )
        url = f"https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz"
        downloaded = _download_atomic(
            [url],
            target,
            prefix="node-runtime-",
            suffix=".tar.xz",
            validate=tarball_ready,
            label="node runtime",
        )
        print(f"downloaded node runtime tarball: {downloaded}")

    def _ensure_prepare_npm(self) -> bool:
        if shutil.which("npm"):
            return True
        if not tarball_ready(self.config.node_runtime_tarball):
            return False
        node_dir = Path(tempfile.mkdtemp(prefix="tb-prepare-node-", dir="/tmp"))
        self._temporary_paths.append(node_dir)
        with tarfile.open(self.config.node_runtime_tarball) as archive:
            archive.extractall(node_dir)
        npm = next(node_dir.glob("*/bin/npm"), None)
        if npm is None:
            return False
        os.environ["PATH"] = f"{npm.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        return True

    def _prepare_get_pip(self) -> None:
        target = self.config.wheel_dir / "get-pip.py"
        if target.is_file():
            print("[prepare] skip get-pip.py (cached)")
            return
        urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", target)
        print("downloaded get-pip.py")

    def _metadata_url(self, package: str, version: str) -> str:
        return f"{self.config.npm_registry_url.rstrip('/')}/{package}/{version}"

    def _pack_npm_to_cache(
        self,
        npm_spec: str,
        target_basename: str,
        registry_meta_url: str,
        latest_glob: str,
    ) -> None:
        target = self.config.wheel_dir / target_basename
        if shutil.which("npm"):
            if not target.is_file():
                with tempfile.TemporaryDirectory() as temporary_name:
                    completed = subprocess.run(
                        [
                            "npm",
                            "pack",
                            "--registry",
                            self.config.npm_registry_url,
                            npm_spec,
                        ],
                        cwd=temporary_name,
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    package_name = completed.stdout.splitlines()[-1].strip()
                    shutil.move(
                        str(Path(temporary_name) / package_name),
                        self.config.wheel_dir / package_name,
                    )
            candidates = sorted(
                self.config.wheel_dir.glob(latest_glob),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                shutil.copy2(candidates[0], target)
            return

        if target.is_file():
            print(f"[prepare] skip {target_basename} (cached)")
            return
        metadata = _read_json(registry_meta_url)
        dist = metadata["dist"]
        if not isinstance(dist, dict):
            raise TypeError(f"invalid npm metadata from {registry_meta_url}")
        url = str(dist["tarball"])
        urllib.request.urlretrieve(url, target)
        print(f"downloaded npm tarball: {url}")

    def _download_npm_tgz_to_cache(
        self, target_basename: str, registry_meta_url: str
    ) -> None:
        target = self.config.wheel_dir / target_basename
        if tarball_ready(target):
            print(f"[prepare] skip {target_basename} (cached)")
            return
        target.unlink(missing_ok=True)
        metadata = _read_json(registry_meta_url)
        dist = metadata["dist"]
        if not isinstance(dist, dict):
            raise TypeError(f"invalid npm metadata from {registry_meta_url}")
        urls = npm_tarball_urls(str(dist["tarball"]), self.config.npm_registry_url)
        downloaded = _download_atomic(
            urls,
            target,
            prefix="npm-tgz-",
            suffix=".tgz",
            validate=tarball_ready,
            label="npm tarball",
            timeout=120,
        )
        print(f"downloaded npm tarball: {downloaded}")

    def _prepare_claude_npm_cache(self) -> None:
        cache = self.config.claude_npm_cache_dir
        if (cache / "_cacache").is_dir() and self._manifest_has(
            f"claude_npm_cache_version={self.config.claude_code_version}"
        ):
            print("[prepare] skip Claude npm cache (cached)")
            return
        self._ensure_prepare_npm()
        if not shutil.which("npm"):
            raise RuntimeError("npm not found; cannot prepare Claude npm cache")
        shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as temporary_name:
            subprocess.run(
                [
                    "npm",
                    "install",
                    "--registry",
                    self.config.npm_registry_url,
                    "--cache",
                    str(cache),
                    "--ignore-scripts",
                    "--no-audit",
                    "--fund=false",
                    self.config.claude_code_npm_spec,
                ],
                cwd=temporary_name,
                check=True,
            )


def main() -> int:
    DependencyPreparer(Config.from_environment()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
