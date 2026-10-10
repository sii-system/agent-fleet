"""Build one OCI image and own its temporary inputs and output lifetime."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..oci import oci_archive_image_config
from ..registry import log
from ..source_urls import validate_source_url
from .base_images import resolve_base_image_contexts
from .dockerfile_renderer.renderer import render_build_dockerfile
from .dockerfile_renderer.strategies.apt import materialize_apt_runtime_assets
from .dockerfile_renderer.strategies.conda import materialize_conda_config
from .dockerfile_renderer.strategies.download import materialize_download_runtime_assets
from .dockerfile_renderer.strategies.git import (
    GITHUB_MIRROR_CONFIG_MOUNT_ID,
    github_mirror_config_content,
)
from .dockerfile_renderer.strategies.packages import materialize_package_source_context
from .executor import run_build
from .frontend import prepare_frontend


@dataclass(frozen=True)
class BuiltImage:
    """The archive is available only inside the build_image context."""

    archive_path: Path
    config: dict[str, object]


@contextmanager
def build_image(
    *,
    context_dir: Path,
    dockerfile: Path,
    temporary_root: Path,
    log_path: Path,
    platform: str,
    timeout_sec: float,
    build_args: dict[str, str],
    package_build_args: dict[str, str],
    dockerhub_mirror_prefix: str = "",
    base_image_registry: str = "",
    download_source_url: str = "",
    github_mirror_url: str = "",
    rustup_init_url: str = "",
    pytorch_index_url: str = "",
    target: str | None = None,
    no_cache: bool = False,
    build_network: str = "default",
) -> Iterator[BuiltImage]:
    """Render, build, and read OCI metadata; clean up after consumption or failure.

    The caller publishes or copies the archive inside the context. Registry
    policy, task identity, and Bundle definitions are not build inputs.
    Logs live at the caller's log_path and survive temporary artifact cleanup.
    """
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="image-build-", dir=temporary_root
    ) as temporary_dir:
        temporary = Path(temporary_dir)
        rendered_package_args = dict(package_build_args)
        for name in ("CONDA_DEFAULTS_URL", "CONDA_CHANNELS_URL"):
            value = build_args.get(name, package_build_args.get(name, ""))
            if value:
                value = validate_source_url(value, "conda channel root", build_network)
            if name in package_build_args or value:
                rendered_package_args[name] = value
        conda_defaults_url = rendered_package_args.get("CONDA_DEFAULTS_URL", "")
        conda_channels_url = rendered_package_args.get("CONDA_CHANNELS_URL", "")
        build_context, rewritten_scripts = materialize_package_source_context(
            context_dir,
            temporary / "context",
            rustup_init_url=rustup_init_url,
            pytorch_index_url=pytorch_index_url,
            conda_defaults_url=conda_defaults_url,
            conda_channels_url=conda_channels_url,
        )
        if rewritten_scripts:
            log(
                "package source configuration rewrote an exact origin URL in "
                f"{len(rewritten_scripts)} build script(s)"
            )
        dockerfile_source = dockerfile.read_text(encoding="utf-8")
        rendered_dockerfile = temporary / "Dockerfile"
        (
            base_image_replacements,
            base_image_contexts,
        ) = resolve_base_image_contexts(
            dockerfile_source,
            base_image_registry,
            platform,
        )
        if base_image_contexts:
            log(
                "resolved base images through configured registry "
                f"count={len(base_image_contexts)}"
            )
        github_mirror_config = github_mirror_config_content(github_mirror_url)
        github_mirror_config_path: Path | None = None
        if github_mirror_config:
            github_mirror_config_path = temporary / "gitconfig"
            github_mirror_config_path.write_text(github_mirror_config, encoding="utf-8")
        runtime_secrets = materialize_apt_runtime_assets(
            temporary / "apt-runtime",
            download_source_url,
        )
        runtime_secrets.update(materialize_conda_config(
            temporary / "conda-runtime", conda_defaults_url, conda_channels_url
        ))
        if download_source_url:
            runtime_secrets.update(
                materialize_download_runtime_assets(
                    temporary / "download-runtime",
                    download_source_url,
                )
            )
        if github_mirror_config_path is not None:
            git_config_digest = hashlib.sha256(
                github_mirror_config_path.read_bytes()
            ).hexdigest()
            git_config_id = f"{GITHUB_MIRROR_CONFIG_MOUNT_ID}-{git_config_digest}"
            runtime_secrets[git_config_id] = github_mirror_config_path
        rendered_dockerfile.write_text(
            render_build_dockerfile(
                dockerfile_source,
                dockerhub_mirror_prefix=dockerhub_mirror_prefix,
                package_build_args=rendered_package_args,
                rustup_init_url=rustup_init_url,
                pytorch_index_url=pytorch_index_url,
                base_image_replacements=base_image_replacements,
            ),
            encoding="utf-8",
        )
        archive_path = temporary / "image.oci.tar"
        frontend_contexts = dict(base_image_contexts)
        effective_build_args = {
            **build_args,
            **prepare_frontend(
                runtime_secrets,
                temporary / "apt-runtime",
                build_contexts=frontend_contexts,
                github_mirror_url=github_mirror_url,
            ),
        }
        log(f"building image platform={platform}; log={log_path}")
        run_build(
            environment_dir=build_context,
            dockerfile=rendered_dockerfile,
            archive_path=archive_path,
            log_path=log_path,
            platform=platform,
            timeout_sec=timeout_sec,
            build_args=effective_build_args,
            build_contexts=frontend_contexts,
            target=target,
            no_cache=no_cache,
            build_network=build_network,
            secret_files=runtime_secrets,
        )
        local_image_config = oci_archive_image_config(archive_path)
        yield BuiltImage(archive_path=archive_path, config=local_image_config)
