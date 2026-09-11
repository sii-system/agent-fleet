from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from swe_rebench_v2.instance_spec import InstanceSpec

# Environment rendering is adapted from the official SWE-rebench-V2 builder's
# combine.Dockerfile.j2 at commit c71902a8cf8d2b725f63d51f199f4d3e56f68d2d:
# https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/combine.Dockerfile.j2

CLONE_REPOSITORY_OVERRIDES = {
    # The archived repository does not contain this dataset commit. Its history
    # was merged into Magistrala, which still serves the exact SHA.
    (
        "absmach/supermq",
        "1c0400d3a58409d4148d2f3cd7befd279dd97a42",
    ): "absmach/magistrala",
    # GitHub no longer redirects the deleted petermattis/pebble path. The
    # migrated CockroachDB repository retains these exact dataset commits.
    (
        "petermattis/pebble",
        "b9be2e7eb20c3b1c04ae5760527dde2a44b3b096",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "4d7ef68ab4c9a0154948e449a185c651fc66ab9c",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "8706ee31debea13e12a82d76bae045aa863447a1",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "2d51c6eb4acc92c40b5a9dc1e518b1035ecd73ef",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "48370d7d34df112c1c8baab25299028f799257c2",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "a7ffd710039b20ac758accaf6e1d2038fe0459ab",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "965163f446adf7122c4a0a129f4d4a19cbd93c94",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "2451b8431268ebfaa39a7d6328e3ec8ae979a072",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "4aeaa2d922ed9243f500bdacec32ff4ca0b2f8ed",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "4c511ec1d202805a4c9727010a543cd763a96a3c",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "0c1c913d7f4719701317fd793861c4660562bb0f",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "fcb9ed13025a2407eb66dd0bc6b370868b52cc17",
    ): "cockroachdb/pebble",
    (
        "petermattis/pebble",
        "53f7531cb726299eb33b5849ae6935501f357442",
    ): "cockroachdb/pebble",
    # The deleted rickbergfalk/sqlpad path likewise has no GitHub redirect;
    # sqlpad/sqlpad retains each exact commit used by the dataset.
    (
        "rickbergfalk/sqlpad",
        "ba30b4e247a91327568b93dfb84bc0a7af2c8fc9",
    ): "sqlpad/sqlpad",
    (
        "rickbergfalk/sqlpad",
        "ff34735a1b681743a6a74b5598bfa4607666f46c",
    ): "sqlpad/sqlpad",
    (
        "rickbergfalk/sqlpad",
        "b6e793cff3d4c2f7b7458ba5a5b312b3204a062d",
    ): "sqlpad/sqlpad",
    (
        "rickbergfalk/sqlpad",
        "edd6efb03a3745dbf80e226ee7fc050c4f2c2d29",
    ): "sqlpad/sqlpad",
}

# The published dataset names this base ``php:8.3.16``, which selects the
# unextended PHP parent image.  The pinned upstream builder instead provides
# ``Dockerfile_php_8.3.16`` as ``php_8.3.16``; that image installs Git before
# the instance template's mandatory pre-install ``git clone``.  Keep the raw
# dataset metadata unchanged and correct only the generated build dependency.
BASE_IMAGE_NAME_OVERRIDES = {
    "php:8.3.16": "php_8.3.16",
}


def _resolved_base_image_name(spec: InstanceSpec) -> str:
    return BASE_IMAGE_NAME_OVERRIDES.get(spec.base_image_name, spec.base_image_name)


def resolve_base_image(spec: InstanceSpec, registry_prefix: str = "") -> str:
    image_name = _resolved_base_image_name(spec)
    prefix = registry_prefix.strip().rstrip("/")
    if not prefix:
        return image_name
    return f"{prefix}/{image_name}"


def render_environment_dockerfile(
    spec: InstanceSpec,
    template_path: Path,
    registry_prefix: str = "",
) -> str:
    raw = deepcopy(spec.raw)
    install_config = dict(raw.get("install_config") or {})
    install_config["image_name"] = _resolved_base_image_name(spec)
    install_config["install"] = list(spec.install_commands)
    raw["install_config"] = install_config
    raw["clone_repo"] = CLONE_REPOSITORY_OVERRIDES.get(
        (spec.repo, spec.base_commit), spec.repo
    )

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    template = env.get_template(template_path.name)
    return template.render(
        spec=raw,
        base_image_registry=registry_prefix.strip().rstrip("/"),
        platform="linux/amd64",
    )
