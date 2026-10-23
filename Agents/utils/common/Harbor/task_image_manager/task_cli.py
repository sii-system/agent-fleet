#!/usr/bin/env python3
"""Resolve task images and prepare a Harbor task bundle.

Flow:

    Harbor selects one local task
                  |
                  v
    Normalize Dockerfile or Compose into named services
                  |
                  v
    List the task repository's existing Registry tags
                  |
                  v
         +--------------------------+
         | Published task tag exists?|
         +-------------+------------+
                       |
              +--------+--------+
              | yes             | no
              v                 v
       Reuse newest tag  Hash static environment files
                              |
                              v
                     Attach build-only source adapters
                              |
                              v
                         Build OCI archive
                              |
                              v
                     skopeo copy + inspect
                              |
              +---------------+
              v
    Write a versioned immutable Bundle Manifest
                  |
                  v
    Return the main image ref for legacy callers

Each benchmark is a Registry Project and each task has its own repository.
On-demand consumers select the newest push_time and validate the selected tag
against the local content-hash prefix by default. Opt out with
--no-validate-image-hash or HARBOR_TASK_IMAGE_VALIDATE_HASH=0 to skip
hashing, warning that local task definitions may differ from remote images.
Equal times are ordered by tag name and digest. Compose services
retain their service-specific tags. Registry digests remain the immutable runtime
addresses. Content-derived tags support build/push, prebuild upkeep, and
consumer validation.
Single-Dockerfile tasks are represented as one implicit ``main`` service.
Dataset prebuild may additionally trust a persistent local uploaded-Bundle
index, with an explicit option to skip the otherwise-default content-hash check.

This CLI prepares images and a Bundle manifest independently of Sandbox startup.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from pathlib import Path

# Support an absolute script path from launchers without requiring PYTHONPATH.
if not __package__:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "task_image_manager"

from .build_engine import (
    APT_RUNTIME_ASSET_DIR,
    APT_RUNTIME_ASSET_NAMES,
    DEFAULT_CARGO_REGISTRY_URL,
    DEFAULT_GOPROXY,
    DEFAULT_GOSUMDB,
    DEFAULT_NPM_REGISTRY,
    DEFAULT_PIP_INDEX_URL,
    DEFAULT_PLATFORM,
    DEFAULT_RUSTUP_DIST_SERVER,
    DEFAULT_RUSTUP_UPDATE_ROOT,
    apt_gateway_root_content,
    apt_runtime_asset_digest,
    apt_runtime_secret_ids,
    base_image_lookup_ref,
    base_image_registry_host,
    dockerfile_external_base_images,
    github_mirror_config_content,
    materialize_apt_runtime_assets,
    materialize_download_runtime_assets,
    mirror_image_ref,
    normalize_base_image_registry,
    package_source_build_args,
    package_source_hosts,
    proxy_build_args,
    render_build_dockerfile,
    resolve_base_image_contexts,
    run_build,
    validate_download_source_url,
    validate_github_mirror_url,
)
from .build_engine.frontend import FrontendBuildError
from .oci import (
    DOCKER_CONFIG,
    DOCKER_LAYER_GZIP,
    DOCKER_MANIFEST,
    OCI_CONFIG,
    OCI_LAYER_GZIP,
    normalize_oci_image_config,
    oci_archive_image_config,
    schema2_manifest,
)
from .registry import (
    RegistryClient,
    RegistryTarget,
    SkopeoPublisher,
    check_task_repository,
    log,
)
from .task_bundle.manifest import PreparedBundle
from .task_image_identity import environment_content_hash, image_identity
from .task_preparation import (
    prepare_task_images,
)

__all__ = [
    "APT_RUNTIME_ASSET_DIR",
    "APT_RUNTIME_ASSET_NAMES",
    "DEFAULT_CARGO_REGISTRY_URL",
    "DEFAULT_GOPROXY",
    "DEFAULT_GOSUMDB",
    "DEFAULT_NPM_REGISTRY",
    "DEFAULT_PIP_INDEX_URL",
    "DEFAULT_PLATFORM",
    "DEFAULT_RUSTUP_DIST_SERVER",
    "DEFAULT_RUSTUP_UPDATE_ROOT",
    "DOCKER_CONFIG",
    "DOCKER_LAYER_GZIP",
    "DOCKER_MANIFEST",
    "OCI_CONFIG",
    "OCI_LAYER_GZIP",
    "PreparedBundle",
    "RegistryClient",
    "RegistryTarget",
    "SkopeoPublisher",
    "apt_gateway_root_content",
    "apt_runtime_asset_digest",
    "apt_runtime_secret_ids",
    "base_image_lookup_ref",
    "base_image_registry_host",
    "check_task_repository",
    "dockerfile_external_base_images",
    "environment_content_hash",
    "github_mirror_config_content",
    "image_identity",
    "log",
    "main",
    "materialize_apt_runtime_assets",
    "materialize_download_runtime_assets",
    "mirror_image_ref",
    "normalize_base_image_registry",
    "normalize_oci_image_config",
    "oci_archive_image_config",
    "package_source_build_args",
    "package_source_hosts",
    "parse_args",
    "prepare",
    "prepare_task_images",
    "proxy_build_args",
    "render_build_dockerfile",
    "resolve_base_image_contexts",
    "run_build",
    "schema2_manifest",
    "validate_download_source_url",
    "validate_github_mirror_url",
]


def default_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else fallback


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare content-addressed Harbor task service images and "
            "an immutable environment bundle"
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task-dir", type=Path)
    source.add_argument("--dataset-root", type=Path)
    parser.add_argument(
        "--include",
        default=os.environ.get(
            "HARBOR_INCLUDE_TASKS", os.environ.get("INCLUDE_TASKS", "")
        ),
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_PACKAGE_SOURCE_HEALTH_URL", ""),
        help="legacy source health endpoint; selects domestic defaults if unavailable and no Gateway is configured"
    )
    parser.add_argument(
        "--probe-timeout-sec",
        type=float,
        default=os.environ.get("HARBOR_TASK_IMAGE_PACKAGE_SOURCE_PROBE_TIMEOUT_SEC", "5"),
        help="legacy health probe timeout in seconds (default: 5, maximum: 60)",
    )
    parser.add_argument(
        "--dependency-gateway-url",
        default=os.environ.get("DEPENDENCY_GATEWAY_URL", ""),
        help="dependency-gateway origin or /v1/cache root; probe before replacing build sources (empty disables)",
    )
    parser.add_argument(
        "--dependency-gateway-timeout-sec",
        type=float,
        default=os.environ.get("HARBOR_TASK_IMAGE_GATEWAY_TIMEOUT_SEC", "5"),
        help="Gateway health probe timeout in seconds (default: 5, maximum: 60)",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("YICLOUD_HARBOR_HOST", ""),
        help="target OCI registry host; defaults to YICLOUD_HARBOR_HOST",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("YICLOUD_HARBOR_PROJECT", ""),
        help="pre-created Registry Project for this benchmark",
    )
    parser.add_argument(
        "--task-repository",
        default=os.environ.get("YICLOUD_HARBOR_TASK_REPOSITORY", ""),
        help="optional controlled task repository override",
    )
    parser.add_argument(
        "--benchmark-name",
        default=os.environ.get("HARBOR_TASK_IMAGE_BENCHMARK", ""),
        help="source benchmark name recorded in the Bundle",
    )
    # Compatibility-only migration input. It is deliberately not used as a
    # target repository: split project/repository forms are rejected unless
    # the project can be unambiguously derived.
    parser.add_argument(
        "--repository",
        default=os.environ.get("HARBOR_TASK_IMAGE_REPOSITORY", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--sandbox-image-prefix", default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--docker-config",
        type=Path,
        default=default_path(
            "HARBOR_TASK_IMAGE_DOCKER_CONFIG", Path.home() / ".docker" / "config.json"
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=default_path(
            "HARBOR_TASK_IMAGE_CACHE_ROOT",
            Path("/data/harbor-runs/opensandbox-images"),
        ),
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("HARBOR_TASK_IMAGE_PLATFORM", DEFAULT_PLATFORM),
        help=f"target image platform; currently only {DEFAULT_PLATFORM} is implemented",
    )
    parser.add_argument(
        "--dockerhub-mirror-prefix",
        default=os.environ.get(
            "HARBOR_TASK_IMAGE_DOCKERHUB_MIRROR_PREFIX", "m.daocloud.io/docker.io"
        ),
    )
    parser.add_argument(
        "--base-image-registry",
        default=os.environ.get("HARBOR_TASK_IMAGE_BASE_IMAGE_REGISTRY", ""),
        help=(
            "optional OCI registry/repository prefix that resolves logical "
            "unqualified or registry-qualified FROM names as immutable "
            "BuildKit named contexts; empty keeps mirror-based resolution"
        ),
    )
    parser.add_argument(
        "--download-source-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_DOWNLOAD_SOURCE_URL", ""),
        help=(
            "optional provider-neutral source root for curl/wget and APT HTTP(S) "
            "downloads; original URLs are translated by fixed dynamic route rules"
        ),
    )
    parser.add_argument(
        "--pip-index-url",
        default=os.environ.get(
            "HARBOR_TASK_IMAGE_PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL
        ),
    )
    parser.add_argument(
        "--npm-registry",
        default=os.environ.get("HARBOR_TASK_IMAGE_NPM_REGISTRY", DEFAULT_NPM_REGISTRY),
    )
    parser.add_argument(
        "--goproxy",
        default=os.environ.get("HARBOR_TASK_IMAGE_GOPROXY", DEFAULT_GOPROXY),
    )
    parser.add_argument(
        "--gosumdb",
        default=os.environ.get("HARBOR_TASK_IMAGE_GOSUMDB", DEFAULT_GOSUMDB),
    )
    parser.add_argument(
        "--cargo-registry-url",
        default=os.environ.get(
            "HARBOR_TASK_IMAGE_CARGO_REGISTRY_URL",
            DEFAULT_CARGO_REGISTRY_URL,
        ),
    )
    parser.add_argument(
        "--rustup-dist-server",
        default=os.environ.get(
            "HARBOR_TASK_IMAGE_RUSTUP_DIST_SERVER",
            DEFAULT_RUSTUP_DIST_SERVER,
        ),
    )
    parser.add_argument(
        "--rustup-update-root",
        default=os.environ.get(
            "HARBOR_TASK_IMAGE_RUSTUP_UPDATE_ROOT",
            DEFAULT_RUSTUP_UPDATE_ROOT,
        ),
    )
    parser.add_argument(
        "--pub-hosted-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_PUB_HOSTED_URL", ""),
        help=(
            "optional build-only Dart Pub hosted source; accepts any "
            "build-reachable HTTP(S) package service"
        ),
    )
    parser.add_argument(
        "--julia-pkg-server",
        default=os.environ.get("HARBOR_TASK_IMAGE_JULIA_PKG_SERVER", ""),
        help=(
            "optional build-only Julia package server; accepts any "
            "build-reachable HTTP(S) package service"
        ),
    )
    parser.add_argument(
        "--github-mirror-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_GITHUB_MIRROR_URL", ""),
        help=(
            "optional build-only GitHub Smart HTTP mirror prefix; applies to "
            "clone, fetch, and recursive submodules"
        ),
    )
    parser.add_argument(
        "--rustup-init-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_RUSTUP_INIT_URL", ""),
        help="optional trusted replacement for exact sh.rustup.rs bootstrap URLs",
    )
    parser.add_argument(
        "--pytorch-index-url",
        default=os.environ.get("HARBOR_TASK_IMAGE_PYTORCH_INDEX_URL", ""),
        help="optional trusted replacement for exact download.pytorch.org/whl URLs",
    )
    parser.add_argument(
        "--package-source-timeout-sec",
        type=int,
        default=int(
            os.environ.get("HARBOR_TASK_IMAGE_PACKAGE_SOURCE_TIMEOUT_SEC", "300")
        ),
    )
    parser.add_argument(
        "--build-args-json",
        default=os.environ.get("HARBOR_TASK_IMAGE_BUILD_ARGS_JSON", "{}"),
    )
    parser.add_argument(
        "--registry-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("YICLOUD_HARBOR_TLS_VERIFY", "0")
        in {"1", "true", "TRUE"},
        help="verify the configured Registry TLS certificate",
    )
    parser.add_argument(
        "--bundle-manifest-output",
        type=Path,
        help="atomically write the prepared Bundle Manifest to this path",
    )
    parser.add_argument(
        "--output",
        choices=("image-ref", "bundle-manifest", "json"),
        default="image-ref",
        help="stdout contract; image-ref preserves the v1 CLI behavior",
    )
    parser.add_argument(
        "--build-timeout-sec",
        type=float,
        help="override task.toml build_timeout_sec for this image preparation",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--validate-image-hash",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("HARBOR_TASK_IMAGE_VALIDATE_HASH", "1").lower()
        in {"1", "true"},
        help=(
            "validate the selected remote image tag against the local task content-hash "
            "prefix before reuse (default: enabled; HARBOR_TASK_IMAGE_VALIDATE_HASH)"
        ),
    )
    parser.add_argument(
        "--reuse-local-upload-cache",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "reuse a target-scoped local record after verifying the current task "
            "content hash, avoiding a Registry cache lookup"
        ),
    )
    parser.add_argument(
        "--skip-hash-verification",
        action="store_true",
        default=False,
        help=(
            "trust a matching local uploaded-Bundle record without hashing task "
            "content; requires --reuse-local-upload-cache"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable BuildKit layer cache for this build without changing image identity",
    )
    parser.add_argument(
        "--build-network",
        choices=("default", "host"),
        default=os.environ.get("HARBOR_TASK_IMAGE_BUILD_NETWORK", "host"),
        help="network mode for Dockerfile RUN instructions",
    )
    parser.add_argument(
        "--use-proxy",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("HARBOR_TASK_IMAGE_BUILD_USE_PROXY", "1")
        in {"1", "true", "TRUE"},
        help="pass the current shell HTTP(S) proxy to Dockerfile builds (default: enabled)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.registry = args.registry.strip().removeprefix("https://").rstrip("/")
    args.project = args.project.strip().strip("/")
    args.task_repository = args.task_repository.strip().strip("/")
    args.benchmark_name = args.benchmark_name.strip()
    legacy_repository = args.repository.strip().strip("/")
    if legacy_repository and not args.project:
        parser.error(
            "legacy --repository does not describe the supported Harbor layout; "
            "set --project and let the task repository be derived"
        )
    if not args.project:
        parser.error("--project or YICLOUD_HARBOR_PROJECT is required")
    if "/" in args.project:
        parser.error("--project must be a single Registry Project name")
    if args.task_repository and "/" in args.task_repository:
        parser.error("--task-repository must be a single repository name")
    if args.skip_hash_verification and not args.reuse_local_upload_cache:
        parser.error("--skip-hash-verification requires --reuse-local-upload-cache")
    return args


def prepare(args: argparse.Namespace) -> str:
    """Backward-compatible single string result used by existing callers."""
    return prepare_task_images(args).main_image_ref


def main() -> int:
    args = parse_args()
    try:
        prepared = prepare_task_images(args)
    except FrontendBuildError as exc:
        log(f"fatal: {exc}")
        return 78  # Shared prerequisite failure; prebuild stops dispatching tasks.
    if args.output == "image-ref":
        print(prepared.main_image_ref)
    elif args.output == "bundle-manifest":
        if prepared.manifest_path is None:
            raise ValueError(
                "--output bundle-manifest requires --bundle-manifest-output "
                "in dry-run mode"
            )
        print(prepared.manifest_path)
    else:
        print(
            json.dumps(
                {
                    "bundle_manifest_path": (
                        str(prepared.manifest_path)
                        if prepared.manifest_path is not None
                        else None
                    ),
                    "main_image_ref": prepared.main_image_ref,
                    "bundle_identity": prepared.manifest["bundle_identity"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    # This is the CLI boundary: report any operational failure without a
    # traceback while preserving KeyboardInterrupt and other BaseExceptions.
    except Exception as exc:  # noqa: BLE001
        log(f"failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
