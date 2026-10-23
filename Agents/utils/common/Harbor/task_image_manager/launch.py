"""Select existing task images or plan the launcher's single-task CLI call.

This module uses only local inputs. Shell owns process execution and Docker
readiness; task_cli.py continues to own image construction and publication.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from qz_template_mapping import QzTemplateMappingError, load_task_image


class ImageSelectionError(ValueError):
    """Local image inputs cannot satisfy the launcher's image handoff."""


@dataclass(frozen=True)
class ImageSelection:
    image_ref: str = ""
    bundle_manifest: str = ""
    manager_args: tuple[str, ...] = ()


def task_image_ref(config: Mapping[str, str]) -> str:
    if config.get("HARBOR_NATIVE_CONCURRENCY") == "1":
        return ""
    task = Path(config.get("DATASET_PATH", "")) / config.get("HARBOR_INCLUDE_TASKS", "")
    if not (task / "task.toml").is_file():
        return ""
    try:
        ref = load_task_image(task)
    except QzTemplateMappingError as error:
        if "missing environment.docker_image" in str(error):
            return ""
        raise ImageSelectionError("failed to read Harbor task image from task.toml") from error
    if any(character.isspace() for character in ref):
        raise ImageSelectionError("task docker_image must be a single image reference")
    return ref


def configured_images(config: Mapping[str, str]) -> ImageSelection:
    ref = config.get("HARBOR_OPENSANDBOX_IMAGE_REF", "")
    bundle = config.get("HARBOR_OPENSANDBOX_BUNDLE_MANIFEST", "")
    return ImageSelection(ref or ("" if bundle else task_image_ref(config)), bundle)


def validate_image_preparation(config: Mapping[str, str], images: ImageSelection) -> None:
    if (config.get("HARBOR_NATIVE_CONCURRENCY") != "1"
            and not images.image_ref and not images.bundle_manifest):
        if not config.get("YICLOUD_HARBOR_PROJECT"):
            raise ImageSelectionError("YICLOUD_HARBOR_PROJECT is required when HARBOR_OPENSANDBOX_IMAGE_REF is unset")
        cli = config.get("HARBOR_TASK_IMAGE_CLI", "")
        if not Path(cli).is_file():
            raise ImageSelectionError(f"Harbor task image manager not found: {cli}")


def bundle_main_image(path: Path) -> str:
    """Read the existing v1/v2 main image contract without importing the builder."""
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        main = bundle.get("main") if bundle.get("schema_version") == 2 else bundle.get("main_service")
        services = bundle.get("services") or {}
        image = (services.get(main) or {}).get("image") or {}
        ref = image.get("digest_ref") if bundle.get("schema_version") == 2 else image.get("sandbox_ref")
        if not isinstance(main, str) or not isinstance(ref, str) or not ref:
            raise ValueError("missing main image")
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as error:
        raise ImageSelectionError("failed to resolve main image from OpenSandbox Bundle Manifest") from error
    return ref


def image_preparation(
    config: Mapping[str, str], automatic_bundle: str,
) -> tuple[ImageSelection, list[str]]:
    images = configured_images(config)
    if config.get("HARBOR_NATIVE_CONCURRENCY") == "1":
        if (images.image_ref or images.bundle_manifest
                or config.get("HARBOR_FORCE_BUILD") in {"1", "true"}):
            raise ImageSelectionError(
                "native OpenSandbox runs require per-task prebuilt images, "
                "without global image overrides or force-build"
            )
        return images, []
    if images.bundle_manifest:
        if not Path(images.bundle_manifest).is_file():
            raise ImageSelectionError(f"OpenSandbox Bundle Manifest not found: {images.bundle_manifest}")
        return ImageSelection(
            images.image_ref or bundle_main_image(Path(images.bundle_manifest)), images.bundle_manifest,
        ), [f"using OpenSandbox Bundle Manifest: {images.bundle_manifest}"]
    if images.image_ref:
        messages = [] if config.get("HARBOR_OPENSANDBOX_IMAGE_REF") else [
            f"using prebuilt Harbor task image for OpenSandbox: {images.image_ref}",
        ]
        return images, messages
    root = config.get("DATASET_PATH", "")
    if not root or not Path(root).is_dir():
        raise ImageSelectionError("automatic Harbor task image preparation currently requires a local dataset path")
    options = (
        ("--dataset-root", "DATASET_PATH"),
        ("--include", "HARBOR_INCLUDE_TASKS"),
        ("--registry", "YICLOUD_HARBOR_HOST"),
        ("--project", "YICLOUD_HARBOR_PROJECT"),
        ("--benchmark-name", "HARBOR_TASK_IMAGE_BENCHMARK"),
        ("--docker-config", "HARBOR_TASK_IMAGE_DOCKER_CONFIG"),
        ("--cache-root", "HARBOR_TASK_IMAGE_CACHE_ROOT"),
        ("--platform", "HARBOR_TASK_IMAGE_PLATFORM"),
        ("--tag-prefix", "HARBOR_TASK_IMAGE_TAG_PREFIX"),
        ("--dockerhub-mirror-prefix", "HARBOR_TASK_IMAGE_DOCKERHUB_MIRROR_PREFIX"),
        ("--base-image-registry", "HARBOR_TASK_IMAGE_BASE_IMAGE_REGISTRY"),
        ("--build-args-json", "HARBOR_TASK_IMAGE_BUILD_ARGS_JSON"),
        ("--build-network", "HARBOR_TASK_IMAGE_BUILD_NETWORK"),
    )
    args = [config.get("HARBOR_TASK_IMAGE_CLI", "")]
    for option, key in options:
        args += [option, config.get(key, "")]
    args += ["--bundle-manifest-output", automatic_bundle]
    for key, values, option in (
        ("YICLOUD_HARBOR_TLS_VERIFY", {"1"}, "--registry-tls-verify"),
        ("HARBOR_FORCE_BUILD", {"1", "true"}, "--force"),
        ("HARBOR_TASK_IMAGE_BUILD_USE_PROXY", {"1"}, "--use-proxy"),
        ("HARBOR_DRY_RUN", {"1"}, "--dry-run"),
    ):
        if config.get(key) in values:
            args.append(option)
    return ImageSelection(manager_args=tuple(args)), []


def prepared_images(image_ref: str, bundle_manifest: str) -> ImageSelection:
    if not image_ref:
        raise ImageSelectionError("Harbor task image manager returned an empty image reference")
    if not Path(bundle_manifest).is_file():
        raise ImageSelectionError(f"Harbor task image manager did not write Bundle Manifest: {bundle_manifest}")
    return ImageSelection(image_ref, bundle_manifest)
