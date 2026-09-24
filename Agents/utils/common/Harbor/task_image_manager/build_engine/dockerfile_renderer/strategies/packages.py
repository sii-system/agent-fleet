"""Package-source build arguments and copied-context URL rendering."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ....source_urls import validate_source_url

DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"
DEFAULT_GOPROXY = "https://goproxy.cn,direct"
DEFAULT_GOSUMDB = "sum.golang.google.cn"
DEFAULT_CARGO_REGISTRY_URL = (
    "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"
)
DEFAULT_RUSTUP_DIST_SERVER = "https://mirrors.tuna.tsinghua.edu.cn/rustup"
DEFAULT_RUSTUP_UPDATE_ROOT = "https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"


def package_source_build_args(
    args: argparse.Namespace, build_network: str
) -> dict[str, str]:
    timeout_value = str(getattr(args, "package_source_timeout_sec", 300)).strip()
    try:
        timeout_seconds = int(timeout_value)
    except ValueError as exc:
        raise ValueError(
            "HARBOR_TASK_IMAGE_PACKAGE_SOURCE_TIMEOUT_SEC must be an integer"
        ) from exc
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError(
            "HARBOR_TASK_IMAGE_PACKAGE_SOURCE_TIMEOUT_SEC must be between 1 and 3600"
        )
    pip_index = validate_source_url(
        getattr(args, "pip_index_url", DEFAULT_PIP_INDEX_URL),
        "pip index",
        build_network,
    )
    npm_registry = validate_source_url(
        getattr(args, "npm_registry", DEFAULT_NPM_REGISTRY),
        "npm registry",
        build_network,
    )
    cargo_index = validate_source_url(
        getattr(args, "cargo_registry_url", DEFAULT_CARGO_REGISTRY_URL),
        "Cargo registry",
        build_network,
        sparse=True,
    )
    rustup_dist = validate_source_url(
        getattr(args, "rustup_dist_server", DEFAULT_RUSTUP_DIST_SERVER),
        "rustup dist server",
        build_network,
    )
    rustup_update = validate_source_url(
        getattr(args, "rustup_update_root", DEFAULT_RUSTUP_UPDATE_ROOT),
        "rustup update root",
        build_network,
    )
    pub_hosted_url = getattr(args, "pub_hosted_url", "").strip()
    if pub_hosted_url:
        pub_hosted_url = validate_source_url(
            pub_hosted_url, "Dart Pub hosted source", build_network
        )
    julia_pkg_server = getattr(args, "julia_pkg_server", "").strip()
    if julia_pkg_server:
        julia_pkg_server = validate_source_url(
            julia_pkg_server, "Julia package server", build_network
        )

    goproxy = getattr(args, "goproxy", DEFAULT_GOPROXY).strip()
    if not goproxy:
        raise ValueError("GOPROXY must not be empty")
    for candidate in goproxy.split(","):
        candidate = candidate.strip()
        if candidate not in {"direct", "off"}:
            validate_source_url(candidate, "Go proxy", build_network)

    gosumdb = getattr(args, "gosumdb", DEFAULT_GOSUMDB).strip()
    if not gosumdb:
        raise ValueError("GOSUMDB must not be empty")
    gosumdb_parts = gosumdb.split()
    if len(gosumdb_parts) > 2:
        raise ValueError("GOSUMDB must be 'off', a verifier name, or 'name URL'")
    if len(gosumdb_parts) == 2:
        validate_source_url(gosumdb_parts[1], "Go checksum database", build_network)

    build_args = {
        "CARGO_HTTP_TIMEOUT": str(timeout_seconds),
        "CARGO_REGISTRIES_CRATES_IO_INDEX": cargo_index,
        "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": (
            "sparse" if cargo_index.startswith("sparse+") else "git"
        ),
        "GOPROXY": goproxy,
        "GOSUMDB": gosumdb,
        "NPM_CONFIG_FETCH_TIMEOUT": str(timeout_seconds * 1000),
        "NPM_CONFIG_REGISTRY": npm_registry,
        "PIP_DEFAULT_TIMEOUT": str(timeout_seconds),
        "PIP_INDEX_URL": pip_index,
        "RUSTUP_DIST_SERVER": rustup_dist,
        "RUSTUP_UPDATE_ROOT": rustup_update,
    }
    if pub_hosted_url:
        build_args["PUB_HOSTED_URL"] = pub_hosted_url
    if julia_pkg_server:
        build_args["JULIA_PKG_SERVER"] = julia_pkg_server
    parsed_pip_index = urlparse(pip_index)
    if parsed_pip_index.scheme == "http":
        build_args["PIP_TRUSTED_HOST"] = parsed_pip_index.hostname or ""
    return build_args


def optional_package_source_urls(
    args: argparse.Namespace, build_network: str
) -> tuple[str, str]:
    rustup_init = getattr(args, "rustup_init_url", "").strip()
    pytorch_index = getattr(args, "pytorch_index_url", "").strip()
    if rustup_init:
        rustup_init = validate_source_url(
            rustup_init, "rustup bootstrap", build_network
        )
    if pytorch_index:
        pytorch_index = validate_source_url(
            pytorch_index, "PyTorch index", build_network
        )
    return rustup_init, pytorch_index


def package_source_hosts(build_args: dict[str, str], *additional_urls: str) -> set[str]:
    values = [
        value
        for name, value in build_args.items()
        if not name.startswith("GIT_CONFIG_")
        and name
        not in {
            "CARGO_HTTP_TIMEOUT",
            "CARGO_REGISTRIES_CRATES_IO_PROTOCOL",
            "NPM_CONFIG_FETCH_TIMEOUT",
            "PIP_DEFAULT_TIMEOUT",
        }
    ]
    values.extend(additional_urls)
    hosts: set[str] = set()
    for value in values:
        for candidate in re.split(r"[,\s]+", value):
            candidate = candidate.removeprefix("sparse+").strip()
            if not candidate or candidate in {"direct", "off"}:
                continue
            parsed = urlparse(candidate)
            if parsed.hostname:
                hosts.add(parsed.hostname.lower())
            elif "/" not in candidate and "." in candidate:
                hosts.add(candidate.lower())
    return hosts


def rewrite_package_source_urls(
    source: str,
    *,
    rustup_init_url: str = "",
    pytorch_index_url: str = "",
    shell_context: bool = True,
) -> str:
    """Rewrite the configured package sources in ``source``.

    ``shell_context`` reports whether ``source`` is shell the build actually
    runs. Callers that walk a Dockerfile pass ``False`` for heredoc bodies that
    are file contents (COPY/ADD) so their bytes stay identical, and ``True`` for
    RUN bodies, which the templates use for every install command: a
    ``curl --proto '=https' ... https://sh.rustup.rs | sh`` bootstrap must be
    rewritten there too, otherwise it keeps its https-only restriction and still
    aims at the public origin.
    """

    if not shell_context:
        return source

    rewritten = source
    if rustup_init_url:
        rewritten = rewritten.replace(
            "https://sh.rustup.rs/rustup-init.sh", rustup_init_url
        ).replace("https://sh.rustup.rs", rustup_init_url)
    if pytorch_index_url:
        rewritten = rewritten.replace(
            "https://download.pytorch.org/whl", pytorch_index_url.rstrip("/")
        )
    if rustup_init_url.startswith("http://"):
        rewritten = "".join(
            line.replace("--proto '=https'", "--proto '=http,https'").replace(
                '--proto "=https"', '--proto "=http,https"'
            )
            if rustup_init_url in line
            else line
            for line in rewritten.splitlines(keepends=True)
        )
    return rewritten


def materialize_package_source_context(
    source_dir: Path,
    destination: Path,
    *,
    rustup_init_url: str = "",
    pytorch_index_url: str = "",
) -> tuple[Path, tuple[str, ...]]:
    """Copy and exactly rewrite reviewed package origins in build scripts."""
    if not rustup_init_url and not pytorch_index_url:
        return source_dir, ()
    rewritten: dict[Path, str] = {}
    for path in source_dir.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or (
                path.name != "Dockerfile"
                and path.suffix not in {".sh", ".bash", ".zsh"}
            )
        ):
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = rewrite_package_source_urls(
            original,
            rustup_init_url=rustup_init_url,
            pytorch_index_url=pytorch_index_url,
        )
        if updated != original:
            rewritten[path.relative_to(source_dir)] = updated
    if not rewritten:
        return source_dir, ()
    shutil.copytree(source_dir, destination, symlinks=True)
    for relative_path, content in rewritten.items():
        destination.joinpath(relative_path).write_text(content, encoding="utf-8")
    return destination, tuple(sorted(str(path) for path in rewritten))
