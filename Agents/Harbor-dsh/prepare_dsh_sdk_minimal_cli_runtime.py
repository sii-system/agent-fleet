"""Prepare the pinned DSH CLI runtime used by the SDK-minimal adapter."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    version: str
    runtime_tarball: Path
    version_file: Path
    node_runtime_tarball: Path
    portable_node_runtime_tarball: Path
    npm_registry_url: str
    npm_cache_dir: Path

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        version = _value(values, "DSH_CLI_VERSION", "0.1.2-alpha.2")
        runtime_basename = _value(
            values,
            "DSH_CLI_RUNTIME_BASENAME",
            f"dsh-sdk-minimal-cli-runtime-{version}.tar.gz",
        )
        return cls(
            wheel_dir=wheel_dir,
            version=version,
            runtime_tarball=Path(
                _value(
                    values,
                    "DSH_CLI_RUNTIME_TARBALL",
                    str(wheel_dir / runtime_basename),
                )
            ),
            version_file=Path(
                _value(
                    values,
                    "DSH_CLI_RUNTIME_VERSION_FILE",
                    str(wheel_dir / "dsh-sdk-minimal-cli-runtime.version"),
                )
            ),
            node_runtime_tarball=Path(
                _value(
                    values,
                    "NODE_RUNTIME_TARBALL",
                    str(wheel_dir / "node-runtime.tar.xz"),
                )
            ),
            portable_node_runtime_tarball=Path(
                _value(
                    values,
                    "DSH_CLI_NODE_RUNTIME_TARBALL",
                    str(wheel_dir / "node-runtime.tar.gz"),
                )
            ),
            npm_registry_url=_value(
                values,
                "NPM_REGISTRY_URL",
                _value(values, "NPM_CONFIG_REGISTRY", "https://registry.npmjs.org"),
            ),
            npm_cache_dir=Path(
                _value(
                    values,
                    "DSH_CLI_NPM_CACHE_DIR",
                    str(wheel_dir / "dsh-sdk-minimal-npm-cache"),
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


def runtime_ready(config: Config) -> bool:
    if not tarball_ready(config.runtime_tarball):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.version


def prepare(config: Config) -> None:
    dsh_runtime_ready = runtime_ready(config)
    portable_node_ready = tarball_ready(config.portable_node_runtime_tarball)
    if dsh_runtime_ready and portable_node_ready:
        print(f"[prepare] skip DSH runtime (cached): {config.runtime_tarball}")
        return
    if not tarball_ready(config.node_runtime_tarball):
        raise RuntimeError(
            f"DSH requires a valid Node 22 archive: {config.node_runtime_tarball}"
        )

    config.wheel_dir.mkdir(parents=True, exist_ok=True)
    config.npm_cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-fleet-dsh-", dir="/tmp"
    ) as temporary_name:
        temporary = Path(temporary_name)
        node_root = temporary / "node"
        node_root.mkdir()
        with tarfile.open(config.node_runtime_tarball) as archive:
            archive.extractall(node_root, filter="data")

        node_bin = next(node_root.glob("*/bin/node"), None)
        npm_bin = next(node_root.glob("*/bin/npm"), None)
        if node_bin is None or npm_bin is None:
            raise RuntimeError("Node archive contains no node/npm binaries")
        completed = subprocess.run(
            [str(node_bin), "-p", "process.versions.node.split('.')[0]"],
            check=True,
            text=True,
            capture_output=True,
        )
        if int(completed.stdout.strip()) < 22:
            raise RuntimeError("DSH requires Node 22 or newer")

        if not portable_node_ready:
            config.portable_node_runtime_tarball.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            file_descriptor, temporary_node_tar_name = tempfile.mkstemp(
                prefix=f".{config.portable_node_runtime_tarball.name}.",
                suffix=".tmp",
                dir=config.portable_node_runtime_tarball.parent,
            )
            os.close(file_descriptor)
            temporary_node_tar = Path(temporary_node_tar_name)
            try:
                node_distribution_root = node_bin.parent.parent
                with tarfile.open(temporary_node_tar, "w:gz") as archive:
                    archive.add(
                        node_distribution_root,
                        arcname=node_distribution_root.name,
                    )
                if not tarball_ready(temporary_node_tar):
                    raise RuntimeError("generated portable Node archive is invalid")
                os.replace(
                    temporary_node_tar,
                    config.portable_node_runtime_tarball,
                )
            finally:
                temporary_node_tar.unlink(missing_ok=True)

        if dsh_runtime_ready:
            print(f"[prepare] skip DSH runtime (cached): {config.runtime_tarball}")
            return

        runtime_prefix = temporary / "runtime"
        env = os.environ.copy()
        env["PATH"] = f"{node_bin.parent}{os.pathsep}{env.get('PATH', '')}"
        subprocess.run(
            [
                str(npm_bin),
                "install",
                "--global",
                "--prefix",
                str(runtime_prefix),
                "--registry",
                config.npm_registry_url,
                "--cache",
                str(config.npm_cache_dir),
                "--no-audit",
                "--fund=false",
                f"@deepseek-ai/dsh@{config.version}",
            ],
            check=True,
            env=env,
        )
        dsh_bin = runtime_prefix / "bin" / "dsh"
        completed = subprocess.run(
            [str(dsh_bin), "--version"],
            check=True,
            text=True,
            capture_output=True,
            env={
                **env,
                "PATH": (
                    f"{runtime_prefix / 'bin'}{os.pathsep}"
                    f"{node_bin.parent}{os.pathsep}{env.get('PATH', '')}"
                ),
            },
        )
        if config.version not in completed.stdout.strip():
            raise RuntimeError(
                "prepared DSH runtime version mismatch: "
                f"expected {config.version!r}, got {completed.stdout.strip()!r}"
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
                for child in runtime_prefix.iterdir():
                    archive.add(child, arcname=child.name)
            if not tarball_ready(temporary_tar):
                raise RuntimeError("generated DSH runtime archive is invalid")
            os.replace(temporary_tar, config.runtime_tarball)
        finally:
            temporary_tar.unlink(missing_ok=True)

    temporary_version = config.version_file.with_suffix(".version.tmp")
    temporary_version.write_text(f"{config.version}\n", encoding="utf-8")
    os.replace(temporary_version, config.version_file)
    print(f"[prepare] built DSH runtime: {config.runtime_tarball}")


def main() -> int:
    prepare(Config.from_environment())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
