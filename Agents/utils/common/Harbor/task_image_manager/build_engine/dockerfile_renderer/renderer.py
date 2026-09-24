"""Compose build-only Dockerfile transformations without changing task inputs."""

from __future__ import annotations

from ..base_images import (
    AS_ALIAS,
    DOCKERFILE_INSTRUCTION,
    FROM_LINE,
    HEREDOC_MARKER,
    mirror_image_ref,
)
from .strategies.packages import rewrite_package_source_urls


def render_build_dockerfile(
    source: str,
    *,
    dockerhub_mirror_prefix: str,
    package_build_args: dict[str, str] | None = None,
    rustup_init_url: str = "",
    pytorch_index_url: str = "",
    base_image_replacements: dict[str, str] | None = None,
) -> str:
    package_build_args = package_build_args or {}
    base_image_replacements = base_image_replacements or {}
    output: list[str] = []
    aliases: set[str] = set()
    active_instruction: str | None = None
    heredocs: list[tuple[str, bool]] = []
    # A heredoc body needs structural handling: a RUN body is shell the build
    # runs (task templates put every install command inside
    # `RUN <<-DOCKER_RUN_EOF`), so package sources inside it are rewritten; a
    # COPY/ADD body is file content and stays byte-identical. The renderer
    # only reports that structure via ``shell_context``; which sources a shell
    # body gets rewritten to stays in the strategy.
    for source_line in source.splitlines():
        if heredocs:
            delimiter, strip_tabs = heredocs[0]
            candidate = source_line.lstrip("\t") if strip_tabs else source_line
            if candidate == delimiter:
                # The delimiter line is structure, not shell: emit it verbatim.
                output.append(source_line)
                heredocs.pop(0)
                if not heredocs:
                    active_instruction = None
                continue
            output.append(
                rewrite_package_source_urls(
                    source_line,
                    rustup_init_url=rustup_init_url,
                    pytorch_index_url=pytorch_index_url,
                    shell_context=active_instruction == "RUN",
                )
            )
            continue

        line = rewrite_package_source_urls(
            source_line,
            rustup_init_url=rustup_init_url,
            pytorch_index_url=pytorch_index_url,
        )
        if active_instruction is None:
            instruction = DOCKERFILE_INSTRUCTION.match(source_line)
            if instruction:
                active_instruction = instruction.group("name").upper()

        match = FROM_LINE.match(line)
        if match:
            source_image = match.group("image")
            mirrored_image = base_image_replacements.get(source_image)
            if mirrored_image is None:
                mirrored_image = mirror_image_ref(
                    source_image, dockerhub_mirror_prefix, aliases
                )
            output.append(
                f"{match.group('prefix')}{mirrored_image}{match.group('suffix')}"
            )
            if package_build_args:
                # ARG values affect only Dockerfile RUN instructions. They are
                # intentionally not persisted in the published image config,
                # where a same-host mirror may be unreachable at runtime.
                output.extend(f"ARG {name}" for name in sorted(package_build_args))
            alias_match = AS_ALIAS.search(match.group("suffix"))
            if alias_match:
                aliases.add(alias_match.group("alias"))
        else:
            output.append(line)

        if active_instruction in {"RUN", "COPY", "ADD"}:
            heredocs.extend(
                (item.group("delimiter"), bool(item.group("strip")))
                for item in HEREDOC_MARKER.finditer(source_line)
            )
        if not heredocs and not source_line.rstrip().endswith("\\"):
            active_instruction = None
    return "\n".join(output) + "\n"
