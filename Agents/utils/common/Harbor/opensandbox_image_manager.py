#!/usr/bin/env python3
"""Prepare content-addressed service images and an OpenSandbox task bundle.

Flow:

    Harbor selects one local task
                  |
                  v
    Normalize Dockerfile or Compose into named services
                  |
                  v
    Hash the original static environment files with the Harbor benchmark framework's
    native environment-content algorithm
                  |
                  v
    Resolve each service to a Registry image
                  |
                  v
         +--------------------------+
         | Registry manifest exists?|
         +-------------+------------+
                       |
              +--------+--------+
              | yes             | no
              v                 v
       Reuse manifest    Rewrite source mirrors
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
Deterministic service tags are cache lookup keys; Registry manifest digests are
the immutable runtime addresses.
Single-Dockerfile tasks are represented as one implicit ``main`` service.
Dataset prebuild may additionally trust a persistent local uploaded-Bundle
index, with an explicit option to skip the otherwise-default content-hash check.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from .compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
        ServiceSpec,
        resolve_bundle_spec,
    )
else:
    from compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
        ServiceSpec,
        resolve_bundle_spec,
    )

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 is still used by some H-side tools.
    tomllib = None  # type: ignore[assignment]

try:
    from harbor.environments.definition import environment_content_hash
except ImportError as exc:
    raise RuntimeError(
        "OpenSandbox image preparation requires the Harbor benchmark "
        "framework API harbor.environments.definition.environment_content_hash; "
        "install a supported Harbor runner version"
    ) from exc


DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
DEFAULT_PLATFORM = "linux/amd64"
DEFAULT_APT_MIRROR = "http://mirrors.tuna.tsinghua.edu.cn"
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"
DEFAULT_GOPROXY = "https://goproxy.cn,direct"
DEFAULT_GOSUMDB = "sum.golang.google.cn"
DEFAULT_CARGO_REGISTRY_URL = (
    "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"
)
DEFAULT_RUSTUP_DIST_SERVER = "https://mirrors.tuna.tsinghua.edu.cn/rustup"
DEFAULT_RUSTUP_UPDATE_ROOT = "https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"
GITHUB_GIT_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "git@github.com:",
    "ssh://git@github.com/",
    "git://github.com/",
)
GITHUB_MIRROR_CONFIG_MOUNT_ID = "opensandbox-github-mirror-gitconfig"
APT_MIRROR_STATE_DIR = "/var/lib/.opensandbox-apt-source-state"
BUILD_RENDERER_VERSION = "apt-source-isolation-v4"
SOURCE_OVERRIDE_FETCH_COMMAND = re.compile(
    r"(?:^|\|)\s*(?:RUN\s+)?"
    r"(?:(?:--mount=\S+|[A-Za-z_][A-Za-z0-9_]*=\S+|"
    r"if|then|do|command|exec|sudo|env)\s+)*"
    r"(?:[^\s;&|]+/)?(?:curl|wget)(?=\s|$)",
    re.IGNORECASE,
)
RUN_EXEC_FORM = re.compile(
    r"^(?P<prefix>\s*RUN\s+(?:(?:--mount|--network|--security)=\S+\s+)*)"
    r"(?P<argv>\[.*\])(?P<suffix>\s*)$",
    re.DOTALL | re.IGNORECASE,
)
RUN_SHELL_PREFIX = re.compile(
    r"^\s*RUN\s+(?:(?:--mount|--network|--security)=\S+\s+)*",
    re.IGNORECASE,
)
SHELL_HEREDOC_EXECUTOR = re.compile(
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S+|command|exec|sudo|env)\s+)*"
    r"(?:[^\s;&|]+/)?(?:sh|bash)(?:\s|$)",
    re.IGNORECASE,
)
PIPE_TO_SHELL = re.compile(
    r"\|\s*(?:(?:command|exec|sudo|env)\s+)*"
    r"(?:[^\s;&|]+/)?(?:sh|bash)(?:\s|$)",
    re.IGNORECASE,
)
SHELL_WRAPPED_COMMAND = re.compile(
    r"(?:^|[;&|])\s*(?:RUN\s+"
    r"(?:(?:--mount|--network|--security)=\S+\s+)*)?"
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S+|command|exec|sudo|env)\s+)*"
    r"(?:[^\s;&|]+/)?(?:sh|bash)\s+"
    r"-[A-Za-z]*c[A-Za-z]*\s+"
    r"(?P<command>'[^']*'|\"[^\"]*\")",
    re.IGNORECASE | re.DOTALL,
)
APT_SOURCE_FILE_REFERENCE = re.compile(
    r"/etc/apt/(?:sources\.list\.d(?:/|(?=$|[\s;&|\"']))|"
    r"sources\.list(?=$|[\s;&|\"']))"
)
APT_SOURCE_RESTORE_AWK = (
    "function replace_literal(text, needle, replacement, position) { "
    "position = index(text, needle); "
    "if (!position) return text; "
    "return substr(text, 1, position - 1) replacement "
    "substr(text, position + length(needle)) "
    "} "
    "FILENAME == ARGV[1] { original[FNR] = $0; next } "
    "FILENAME == ARGV[2] { "
    "adapted[FNR] = $0; "
    "adapted_seen[$0]++; "
    "restore[$0 SUBSEP adapted_seen[$0]] = original[FNR]; "
    "next "
    "} "
    "{ "
    "line = $0; "
    "replaced = 0; "
    "original_count = split(original[FNR], original_fields); "
    "adapted_count = split(adapted[FNR], adapted_fields); "
    "if (original_count == adapted_count) { "
    "for (field = 1; field <= adapted_count; field++) { "
    "if (original_fields[field] != adapted_fields[field] "
    "&& index(line, adapted_fields[field])) { "
    "line = replace_literal(line, adapted_fields[field], "
    "original_fields[field]); "
    "replaced = 1 "
    "} "
    "} "
    "} "
    "if (replaced) { print line; next } "
    "current_seen[$0]++; "
    "key = $0 SUBSEP current_seen[$0]; "
    "if (key in restore) print restore[key]; else print "
    "}"
)
BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FROM_LINE = re.compile(
    r"^(?P<prefix>\s*FROM(?:\s+--platform=\S+)?\s+)"
    r"(?P<image>\S+)(?P<suffix>.*)$",
    re.IGNORECASE,
)
AS_ALIAS = re.compile(r"\s+AS\s+(?P<alias>[A-Za-z0-9_.-]+)\s*$", re.IGNORECASE)
DOCKERFILE_INSTRUCTION = re.compile(r"^\s*(?P<name>[A-Za-z]+)\b")
HEREDOC_MARKER = re.compile(
    r"<<(?P<strip>-)?\s*(?P<quote>['\"]?)"
    r"(?P<delimiter>[A-Za-z0-9_.-]+)(?P=quote)"
)
APT_COMMAND = re.compile(
    r"(?:"
    r"(?:^|[;&|])\s*(?:RUN\s+)?"
    r"(?:(?:--mount=\S+|[A-Za-z_][A-Za-z0-9_]*=\S+|"
    r"if|then|do|command|exec|sudo|env)\s+)*"
    r"(?:[^\s;&|]+/)?apt(?:-get)?(?=\s|$)"
    r'|^\s*RUN\s+\[\s*"(?:[^"]*/)?apt(?:-get)?"\s*[,\]]'
    r")",
    re.IGNORECASE,
)

# This is deliberately a small, version-controlled adapter contract rather
# than an inference based on service names or installed software.  It covers
# the real Compose task whose SSH sidecar has OCI evidence for port 22 but no
# image/Compose healthcheck from which readiness can otherwise be derived.
OPENSANDBOX_ADAPTER_METADATA: dict[str, dict[str, dict[str, dict[str, object]]]] = {
    "seta": {
        "973": {
            "worker": {
                "readiness": {"type": "tcp", "port": 22},
            }
        }
    }
}
LEGACY_DOCKERFILE_KEEPALIVE = ["sh", "-c", "while :; do sleep 60; done"]
LOCAL_UPLOAD_INDEX_VERSION = 2
SKOPEO_COPY_ATTEMPTS = 3
SKOPEO_COPY_RETRY_DELAY_SECONDS = 3


def log(message: str) -> None:
    print(f"[opensandbox-image] {message}", file=sys.stderr, flush=True)


def digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def safe_tag_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-").lower()
    return normalized or "task"


def resolve_task_dir(
    task_dir: Path | None, dataset_root: Path | None, include: str
) -> Path:
    if task_dir is not None:
        resolved = task_dir.resolve()
    elif dataset_root is not None:
        root = dataset_root.resolve()
        if (root / "task.toml").is_file():
            resolved = root
        else:
            task_names = [item.strip() for item in include.split(",") if item.strip()]
            if len(task_names) != 1:
                raise ValueError(
                    "automatic OpenSandbox image preparation requires exactly one "
                    "included task"
                )
            resolved = (root / task_names[0]).resolve()
    else:
        raise ValueError("provide --task-dir or --dataset-root")

    if not (resolved / "task.toml").is_file():
        raise ValueError(f"task.toml not found under {resolved}")
    return resolved


def load_build_timeout(task_dir: Path) -> float:
    task_config_path = task_dir / "task.toml"
    if tomllib is not None:
        with task_config_path.open("rb") as handle:
            task_config = tomllib.load(handle)
        value = (task_config.get("environment") or {}).get("build_timeout_sec", 600)
    else:
        value = 600
        section = ""
        for raw_line in task_config_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif section == "environment" and line.startswith("build_timeout_sec"):
                _, raw_value = line.split("=", 1)
                value = raw_value.strip()
                break
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"invalid environment.build_timeout_sec: {value!r}")
    return timeout


def apt_404_requires_cache_refresh(log_path: Path) -> bool:
    try:
        build_log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "404  Not Found" in build_log and any(
        marker in build_log
        for marker in (
            "Failed to fetch",
            "Unable to fetch",
            "does not have a Release file",
        )
    )


def mirror_image_ref(image: str, mirror_prefix: str, aliases: set[str]) -> str:
    if not mirror_prefix or image in aliases or image.startswith("$"):
        return image
    first, separator, remainder = image.partition("/")
    if first == "docker.io" and separator:
        return f"{mirror_prefix.rstrip('/')}/{remainder}"
    if not separator or ("." not in first and ":" not in first and first != "localhost"):
        relative = image if "/" in image else f"library/{image}"
        return f"{mirror_prefix.rstrip('/')}/{relative}"
    return image


def sed_replacement(value: str) -> str:
    """Escape a literal value for a ``#``-delimited sed replacement."""
    return re.sub(r"([\\&#])", r"\\\1", value)


def sed_pattern(value: str) -> str:
    """Escape a literal value for a ``#``-delimited POSIX ERE pattern."""
    return re.sub(r"([\\.^$*+?()\[\]{}|#])", r"\\\1", value)


def ordered_source_overrides(
    source_overrides: dict[str, str], *, reverse: bool = False
) -> tuple[tuple[str, str], ...]:
    pairs = (
        ((target, source) for source, target in source_overrides.items())
        if reverse
        else source_overrides.items()
    )
    return tuple(sorted(pairs, key=lambda item: len(item[0]), reverse=True))


def source_prefix_matches(prefix: str, value: str) -> bool:
    """Return whether ``prefix`` covers ``value`` as the same URL token."""
    return bool(re.match(re.escape(prefix) + r"(?=$|[^A-Za-z0-9._~-])", value))


def rewrite_source_overrides(
    source: str, source_overrides: dict[str, str], *, reverse: bool = False
) -> str:
    """Rewrite configured URL prefixes without matching a longer URL token."""
    rewritten = source
    for original, replacement in ordered_source_overrides(
        source_overrides, reverse=reverse
    ):
        pattern = re.compile(
            re.escape(original) + r"(?=$|[^A-Za-z0-9._~-])"
        )
        rewritten = pattern.sub(lambda _match, value=replacement: value, rewritten)
    return rewritten


def run_heredoc_specs(
    source: str, *, rewrite_apt_sources: bool
) -> list[tuple[str, bool, str]]:
    """Return Dockerfile heredocs and whether their payload is build commands."""
    specs: list[tuple[str, bool, str]] = []
    for marker in HEREDOC_MARKER.finditer(source):
        prefix = re.sub(r"\\\r?\n", " ", source[: marker.start()])
        run_prefix = RUN_SHELL_PREFIX.match(prefix)
        shell_body = prefix[run_prefix.end() :] if run_prefix else prefix
        command_segment = re.split(r"&&|\|\||[;|]", shell_body)[-1].strip()
        line_start = source.rfind("\n", 0, marker.start()) + 1
        line_end = source.find("\n", marker.end())
        if line_end < 0:
            line_end = len(source)
        declaration = source[line_start:line_end]
        payload_is_command = (
            not command_segment
            or SHELL_HEREDOC_EXECUTOR.match(command_segment) is not None
            or PIPE_TO_SHELL.search(declaration[marker.end() - line_start :])
            is not None
        )
        payload_is_apt_source = (
            rewrite_apt_sources
            and APT_SOURCE_FILE_REFERENCE.search(declaration) is not None
        )
        if payload_is_apt_source:
            rewrite_mode = "all"
        elif payload_is_command:
            rewrite_mode = "safe"
        else:
            rewrite_mode = "none"
        specs.append(
            (marker.group("delimiter"), bool(marker.group("strip")), rewrite_mode)
        )
    return specs


def run_invokes_apt(source: str) -> bool:
    """Recognize direct and statically visible shell-wrapped APT commands."""
    source = re.sub(r"\\\r?\n", " ", source)
    if APT_COMMAND.search(source):
        return True

    exec_form = RUN_EXEC_FORM.match(source)
    if exec_form:
        try:
            argv = json.loads(exec_form.group("argv"))
        except ValueError:
            argv = None
        if (
            isinstance(argv, list)
            and argv
            and all(isinstance(argument, str) for argument in argv)
            and argv[0].rsplit("/", 1)[-1].lower() in {"sh", "bash"}
        ):
            for index, argument in enumerate(argv[1:], start=1):
                if (
                    argument.startswith("-")
                    and "c" in argument[1:]
                    and index + 1 < len(argv)
                ):
                    return APT_COMMAND.search(argv[index + 1]) is not None

    return any(
        APT_COMMAND.search(match.group("command")[1:-1]) is not None
        for match in SHELL_WRAPPED_COMMAND.finditer(source)
    )


def rewrite_run_source_overrides(
    source: str,
    source_overrides: dict[str, str],
    *,
    rewrite_apt_sources: bool,
) -> str:
    """Rewrite only unambiguously build-transport URL occurrences in a RUN."""
    if not source_overrides:
        return source

    exec_form = RUN_EXEC_FORM.match(source)
    if exec_form:
        try:
            argv = json.loads(exec_form.group("argv"))
        except ValueError:
            argv = None
        if (
            isinstance(argv, list)
            and argv
            and all(isinstance(argument, str) for argument in argv)
            and argv[0].rsplit("/", 1)[-1].lower() in {"curl", "wget"}
        ):
            rewritten_argv = [
                argv[0],
                *(
                    rewrite_source_overrides(argument, source_overrides)
                    for argument in argv[1:]
                ),
            ]
            return (
                exec_form.group("prefix")
                + json.dumps(rewritten_argv)
                + exec_form.group("suffix")
            )

    output: list[str] = []
    segment_start = 0
    quote: str | None = None
    index = 0

    def append_segment(end: int) -> None:
        segment = source[segment_start:end]
        probe = re.sub(r"\\\r?\n", " ", segment)
        is_fetch = SOURCE_OVERRIDE_FETCH_COMMAND.search(probe) is not None
        is_apt_source_write = (
            rewrite_apt_sources
            and APT_SOURCE_FILE_REFERENCE.search(probe) is not None
        )
        output.append(
            rewrite_source_overrides(segment, source_overrides)
            if is_fetch or is_apt_source_write
            else segment
        )

    while index < len(source):
        character = source[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if character == "\\" and index + 1 < len(source):
                index += 2
                continue
            if character == '"':
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "\\" and index + 1 < len(source):
            index += 2
            continue

        delimiter_length = 0
        if source.startswith(("&&", "||"), index):
            delimiter_length = 2
        elif character in {";", "\n"}:
            delimiter_length = 1
        if not delimiter_length:
            index += 1
            continue

        append_segment(index)
        output.append(source[index : index + delimiter_length])
        index += delimiter_length
        segment_start = index

    append_segment(len(source))
    return "".join(output)


def rewrite_dockerfile_run_source_overrides(
    source: str,
    source_overrides: dict[str, str],
    apt_stages: tuple[bool, ...],
) -> str:
    """Apply source overrides to safe contexts in complete RUN instructions."""
    # TODO: Support configured APT URLs that appear only inside scripts
    # downloaded or generated while a RUN executes.
    if not source_overrides:
        return source

    output: list[str] = []
    run_lines: list[str] = []
    run_rewrite_modes: list[str] = []
    heredocs: list[tuple[str, bool, str]] = []
    stage_index = -1

    def rewrite_chunk_content(lines: list[str], mode: str) -> str:
        content = "".join(lines)
        if mode == "all":
            return rewrite_source_overrides(content, source_overrides)
        if mode == "safe":
            return rewrite_run_source_overrides(
                content,
                source_overrides,
                rewrite_apt_sources=(
                    stage_index < len(apt_stages) and apt_stages[stage_index]
                ),
            )
        return content

    def flush_run() -> None:
        chunk: list[str] = []
        rewrite_chunk: str | None = None
        for line, rewrite_line in zip(run_lines, run_rewrite_modes, strict=True):
            if rewrite_chunk is not None and rewrite_line != rewrite_chunk:
                output.append(rewrite_chunk_content(chunk, rewrite_chunk))
                chunk.clear()
            rewrite_chunk = rewrite_line
            chunk.append(line)
        if chunk:
            output.append(rewrite_chunk_content(chunk, rewrite_chunk or "none"))
        run_lines.clear()
        run_rewrite_modes.clear()

    for source_line in source.splitlines(keepends=True):
        if not run_lines:
            instruction = DOCKERFILE_INSTRUCTION.match(source_line)
            if not instruction:
                output.append(source_line)
                continue
            instruction_name = instruction.group("name").upper()
            if instruction_name == "FROM":
                stage_index += 1
            if instruction_name != "RUN":
                output.append(source_line)
                continue
            run_lines.append(source_line)
            run_rewrite_modes.append("safe")
            heredocs.extend(
                run_heredoc_specs(
                    "".join(run_lines),
                    rewrite_apt_sources=(
                        stage_index < len(apt_stages) and apt_stages[stage_index]
                    ),
                )
            )
        else:
            if heredocs:
                delimiter, strip_tabs, rewrite_mode = heredocs[0]
                candidate = source_line.rstrip("\r\n")
                if strip_tabs:
                    candidate = candidate.lstrip("\t")
                run_lines.append(source_line)
                run_rewrite_modes.append(
                    rewrite_mode if candidate != delimiter else "none"
                )
                if candidate == delimiter:
                    heredocs.pop(0)
            else:
                run_lines.append(source_line)
                run_rewrite_modes.append("safe")
                heredocs.extend(
                    run_heredoc_specs(
                        "".join(run_lines),
                        rewrite_apt_sources=(
                            stage_index < len(apt_stages)
                            and apt_stages[stage_index]
                        ),
                    )
                )

        if not heredocs and not source_line.rstrip().endswith("\\"):
            flush_run()

    if run_lines:
        flush_run()
    return "".join(output)


def source_override_sed(
    source_overrides: dict[str, str], *, reverse: bool = False
) -> str:
    expressions = []
    for original, replacement in ordered_source_overrides(
        source_overrides, reverse=reverse
    ):
        expressions.append(
            f"s#{sed_pattern(original)}([^A-Za-z0-9._~-]|$)#"
            f"{sed_replacement(replacement)}\\1#g"
        )
    return ";".join(expressions)


def apt_mirror_command(
    apt_mirror: str,
    *,
    stage_uses_apt: bool = False,
    source_overrides: dict[str, str] | None = None,
) -> str | None:
    if not apt_mirror or not stage_uses_apt:
        return None
    mirror = sed_replacement(apt_mirror.rstrip("/"))
    source_overrides = source_overrides or {}
    # Keep an exact copy of the stage's authored source files. The matching
    # cleanup instruction restores untouched files byte-for-byte and removes
    # only this adapter's transport rewrite from files the task changed.
    replacements = (
        f"s#https?://(archive|security|ports)\\.ubuntu\\.com/ubuntu/?#"
        f"{mirror}/ubuntu/#g;"
        f"s#https?://(deb|security)\\.debian\\.org/debian-security/?#"
        f"{mirror}/debian-security/#g;"
        f"s#https?://deb\\.debian\\.org/debian/?#{mirror}/debian/#g"
    )
    override_replacements = source_override_sed(source_overrides)
    if override_replacements:
        replacements = f"{replacements};{override_replacements}"
    command = (
        "set -eu; "
        f"state={shlex.quote(APT_MIRROR_STATE_DIR)}; "
        'rm -rf "$state"; '
        'mkdir -p "$state/original" "$state/adapted"; '
        "for path in sources.list sources.list.d; do "
        'source_path="/etc/apt/$path"; '
        'if [ -e "$source_path" ] || [ -L "$source_path" ]; then '
        'cp -a "$source_path" "$state/original/$path"; '
        "fi; "
        "done; "
        "for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
        "/etc/apt/sources.list.d/*.sources; do "
        '[ -f "$file" ] || continue; '
        f"sed -E -i {shlex.quote(replacements)} \"$file\"; "
        "done; "
        "for path in sources.list sources.list.d; do "
        'source_path="/etc/apt/$path"; '
        'if [ -e "$source_path" ] || [ -L "$source_path" ]; then '
        'cp -a "$source_path" "$state/adapted/$path"; '
        "fi; "
        "done"
    )
    return f"RUN {json.dumps(['/bin/sh', '-c', command])}"


def apt_mirror_cleanup_command(
    apt_mirror: str, source_overrides: dict[str, str] | None = None
) -> str:
    """Restore task-visible APT sources after a mirrored build stage."""
    mirror = apt_mirror.rstrip("/")
    mirror_pattern = sed_pattern(mirror)
    source_overrides = source_overrides or {}
    target_format = (
        "$(SOURCESENTRY)~$(IDENTIFIER)~$(RELEASE)~$(COMPONENT)~"
        "$(ARCHITECTURE)~$(CREATED_BY)~$(METAKEY)|$(COMPONENT)|$(FILENAME)"
    )
    replacements = (
        f"s#{mirror_pattern}/ubuntu/?#http://archive.ubuntu.com/ubuntu/#g;"
        f"s#{mirror_pattern}/debian-security/?#"
        "http://security.debian.org/debian-security/#g;"
        f"s#{mirror_pattern}/debian/?#http://deb.debian.org/debian/#g"
    )
    override_replacements = source_override_sed(source_overrides, reverse=True)
    if override_replacements:
        replacements = f"{replacements};{override_replacements}"
    command = (
        "set -eu; "
        f"state={shlex.quote(APT_MIRROR_STATE_DIR)}; "
        '[ -d "$state" ] || exit 0; '
        f"target_format={shlex.quote(target_format)}; "
        "if command -v apt-get >/dev/null 2>&1; then "
        'apt-get indextargets --no-release-info --format "$target_format" '
        '> "$state/adapted-targets" 2>/dev/null || :; '
        "fi; "
        "for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
        "/etc/apt/sources.list.d/*.sources; do "
        '[ -f "$file" ] || continue; '
        'relative="${file#/etc/apt/}"; '
        'adapted="$state/adapted/$relative"; '
        'original="$state/original/$relative"; '
        'if [ -f "$adapted" ] && [ -e "$original" ] '
        '&& cmp -s "$file" "$adapted"; then '
        'rm -f "$file"; cp -a "$original" "$file"; '
        "else "
        'if [ -f "$adapted" ] && [ -e "$original" ] '
        "&& command -v awk >/dev/null 2>&1; then "
        f"awk {shlex.quote(APT_SOURCE_RESTORE_AWK)} "
        '"$original" "$adapted" "$file" > "$state/merged-source"; '
        'cat "$state/merged-source" > "$file"; '
        "fi; "
        f"sed -E -i {shlex.quote(replacements)} \"$file\"; "
        "fi; "
        "done; "
        'if [ -s "$state/adapted-targets" ] '
        "&& command -v apt-get >/dev/null 2>&1 "
        "&& command -v sort >/dev/null 2>&1 "
        "&& command -v join >/dev/null 2>&1; then "
        'apt-get indextargets --no-release-info --format "$target_format" '
        '> "$state/restored-targets" 2>/dev/null || :; '
        'sort "$state/adapted-targets" -o "$state/adapted-targets"; '
        'sort "$state/restored-targets" -o "$state/restored-targets"; '
        'join -t "|" "$state/adapted-targets" "$state/restored-targets" '
        '> "$state/target-moves" || :; '
        'while IFS="|" read -r key source_component source_path '
        "target_component target_path; do "
        '[ "$source_path" = "$target_path" ] && continue; '
        'for candidate in "$source_path" "$source_path".*; do '
        '[ -f "$candidate" ] || continue; '
        'suffix=${candidate#"$source_path"}; '
        'cp -a "$candidate" "$target_path$suffix"; '
        "done; "
        'source_marker=_${source_component}_; '
        'target_marker=_${target_component}_; '
        'source_dist=${source_path%%"$source_marker"*}; '
        'target_dist=${target_path%%"$target_marker"*}; '
        "for release_file in InRelease Release Release.gpg; do "
        '[ -f "${source_dist}_${release_file}" ] '
        '&& cp -a "${source_dist}_${release_file}" '
        '"${target_dist}_${release_file}"; '
        "done; "
        'done < "$state/target-moves"; '
        'while IFS="|" read -r key source_component source_path '
        "target_component target_path; do "
        '[ "$source_path" = "$target_path" ] && continue; '
        'rm -f "$source_path" "$source_path".*; '
        'source_marker=_${source_component}_; '
        'source_dist=${source_path%%"$source_marker"*}; '
        'rm -f "${source_dist}_InRelease" "${source_dist}_Release" '
        '"${source_dist}_Release.gpg"; '
        'done < "$state/target-moves"; '
        "fi; "
        'rm -rf "$state"'
    )
    return f"RUN {json.dumps(['/bin/sh', '-c', command])}"


def append_apt_mirror_cleanup(
    output: list[str],
    apt_mirror: str,
    current_user_instruction: str | None,
    source_overrides: dict[str, str],
) -> None:
    """Append cleanup under root, then restore an explicit task USER."""
    restore_user = current_user_instruction
    if restore_user is not None:
        user_value = restore_user.split(None, 1)[1].split(":", 1)[0].lower()
        if user_value not in {"root", "0"}:
            output.append("USER root")
        else:
            restore_user = None
    output.append(apt_mirror_cleanup_command(apt_mirror, source_overrides))
    if restore_user is not None:
        output.append(restore_user)


def append_apt_mirror_refresh(
    output: list[str],
    apt_mirror: str,
    current_user_instruction: str | None,
    source_overrides: dict[str, str],
) -> None:
    """Restore task sources, snapshot them again, and reapply build mirrors."""
    restore_user = current_user_instruction
    if restore_user is not None:
        user_value = restore_user.split(None, 1)[1].split(":", 1)[0].lower()
        if user_value not in {"root", "0"}:
            output.append("USER root")
        else:
            restore_user = None
    output.append(apt_mirror_cleanup_command(apt_mirror, source_overrides))
    setup = apt_mirror_command(
        apt_mirror,
        stage_uses_apt=True,
        source_overrides=source_overrides,
    )
    if setup is None:
        raise AssertionError("APT mirror refresh requires a configured mirror")
    output.append(setup)
    if restore_user is not None:
        output.append(restore_user)


def dockerfile_apt_stages(source: str) -> tuple[bool, ...]:
    """Identify Dockerfile stages that actually invoke apt or apt-get."""
    stages: list[bool] = []
    active_instruction: str | None = None
    instruction_lines: list[str] = []
    heredocs: list[tuple[str, bool, str]] = []
    for source_line in source.splitlines():
        if heredocs:
            if (
                stages
                and active_instruction == "RUN"
                and heredocs[0][2] == "safe"
                and run_invokes_apt(source_line)
            ):
                stages[-1] = True
            delimiter, strip_tabs, _rewrite_mode = heredocs[0]
            candidate = source_line.lstrip("\t") if strip_tabs else source_line
            if candidate == delimiter:
                heredocs.pop(0)
                if not heredocs:
                    active_instruction = None
                    instruction_lines.clear()
            continue

        instruction = None
        if active_instruction is None:
            instruction = DOCKERFILE_INSTRUCTION.match(source_line)
            if instruction:
                active_instruction = instruction.group("name").upper()
                instruction_lines = [source_line]
            if active_instruction == "FROM":
                stages.append(False)
        else:
            instruction_lines.append(source_line)
        if (
            stages
            and active_instruction == "RUN"
            and run_invokes_apt("\n".join(instruction_lines))
        ):
            stages[-1] = True

        if active_instruction in {"RUN", "COPY", "ADD"}:
            if active_instruction == "RUN":
                heredocs.extend(
                    run_heredoc_specs(
                        "\n".join(instruction_lines),
                        rewrite_apt_sources=False,
                    )
                )
            else:
                heredocs.extend(
                    (
                        item.group("delimiter"),
                        bool(item.group("strip")),
                        "none",
                    )
                    for item in HEREDOC_MARKER.finditer(source_line)
                )
        if not heredocs and not source_line.rstrip().endswith("\\"):
            active_instruction = None
            instruction_lines.clear()
    return tuple(stages)


def _validate_source_url(
    value: str,
    label: str,
    build_network: str,
    *,
    sparse: bool = False,
) -> str:
    normalized = value.strip()
    parsed_value = normalized.removeprefix("sparse+") if sparse else normalized
    parsed = urlparse(parsed_value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label} must be an absolute query-free HTTP(S) URL without credentials"
        )
    host = parsed.hostname.lower()
    # This is an address-scope check, not a service availability probe. With
    # isolated BuildKit networking, loopback names the build container itself.
    if host in {"127.0.0.1", "localhost", "::1"} and build_network != "host":
        raise ValueError(
            f"loopback {label} is unreachable from BuildKit without "
            "--build-network=host; use a build-reachable source instead"
        )
    return normalized


def validate_apt_mirror(apt_mirror: str, build_network: str) -> str:
    """Validate mirror syntax and reject container-unreachable loopback use."""
    normalized = apt_mirror.strip().rstrip("/")
    if not normalized:
        raise ValueError(
            "--apt-mirror or HARBOR_OPENSANDBOX_APT_MIRROR must name an "
            "APT mirror root"
        )
    return _validate_source_url(normalized, "APT mirror", build_network).rstrip("/")


def parse_apt_source_overrides(
    raw: str, build_network: str
) -> dict[str, str]:
    """Parse explicit upstream-to-build-source URL prefix mappings."""
    if not raw.strip():
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise TypeError("APT source overrides must be a JSON object")
    overrides: dict[str, str] = {}
    reverse_targets: dict[str, str] = {}
    for original, replacement in loaded.items():
        if not isinstance(original, str) or not isinstance(replacement, str):
            raise TypeError("APT source override keys and values must be strings")
        normalized_original = _validate_source_url(
            original, "APT source override origin", build_network
        ).rstrip("/")
        normalized_replacement = _validate_source_url(
            replacement, "APT source override replacement", build_network
        ).rstrip("/")
        if normalized_original == normalized_replacement:
            raise ValueError("APT source override must change the source URL")
        if normalized_original in overrides:
            raise ValueError(
                f"duplicate normalized APT source override: {normalized_original}"
            )
        previous_origin = reverse_targets.get(normalized_replacement)
        if previous_origin is not None:
            raise ValueError(
                "APT source override replacements must be unique for cleanup: "
                f"{normalized_replacement} maps both {previous_origin} and "
                f"{normalized_original}"
            )
        overrides[normalized_original] = normalized_replacement
        reverse_targets[normalized_replacement] = normalized_original
    overlap = set(overrides).intersection(reverse_targets)
    if overlap:
        raise ValueError(
            "APT source override origins and replacements must be disjoint: "
            + ", ".join(sorted(overlap))
        )
    cross_prefixes = sorted(
        {
            (origin, replacement)
            for origin in overrides
            for replacement in reverse_targets
            if source_prefix_matches(origin, replacement)
            or source_prefix_matches(replacement, origin)
        }
    )
    if cross_prefixes:
        rendered = ", ".join(
            f"{origin} <> {replacement}"
            for origin, replacement in cross_prefixes
        )
        raise ValueError(
            "APT source override origins and replacements must not overlap "
            f"by URL prefix: {rendered}"
        )
    return overrides


def package_source_build_args(
    args: argparse.Namespace, build_network: str
) -> dict[str, str]:
    timeout_value = str(getattr(args, "package_source_timeout_sec", 300)).strip()
    try:
        timeout_seconds = int(timeout_value)
    except ValueError as exc:
        raise ValueError(
            "HARBOR_OPENSANDBOX_PACKAGE_SOURCE_TIMEOUT_SEC must be an integer"
        ) from exc
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError(
            "HARBOR_OPENSANDBOX_PACKAGE_SOURCE_TIMEOUT_SEC must be between 1 and 3600"
        )
    pip_index = _validate_source_url(
        getattr(args, "pip_index_url", DEFAULT_PIP_INDEX_URL),
        "pip index",
        build_network,
    )
    npm_registry = _validate_source_url(
        getattr(args, "npm_registry", DEFAULT_NPM_REGISTRY),
        "npm registry",
        build_network,
    )
    cargo_index = _validate_source_url(
        getattr(args, "cargo_registry_url", DEFAULT_CARGO_REGISTRY_URL),
        "Cargo registry",
        build_network,
        sparse=True,
    )
    rustup_dist = _validate_source_url(
        getattr(args, "rustup_dist_server", DEFAULT_RUSTUP_DIST_SERVER),
        "rustup dist server",
        build_network,
    )
    rustup_update = _validate_source_url(
        getattr(args, "rustup_update_root", DEFAULT_RUSTUP_UPDATE_ROOT),
        "rustup update root",
        build_network,
    )

    goproxy = getattr(args, "goproxy", DEFAULT_GOPROXY).strip()
    if not goproxy:
        raise ValueError("GOPROXY must not be empty")
    for candidate in goproxy.split(","):
        candidate = candidate.strip()
        if candidate not in {"direct", "off"}:
            _validate_source_url(
                candidate, "Go proxy", build_network
            )

    gosumdb = getattr(args, "gosumdb", DEFAULT_GOSUMDB).strip()
    if not gosumdb:
        raise ValueError("GOSUMDB must not be empty")
    gosumdb_parts = gosumdb.split()
    if len(gosumdb_parts) > 2:
        raise ValueError("GOSUMDB must be 'off', a verifier name, or 'name URL'")
    if len(gosumdb_parts) == 2:
        _validate_source_url(
            gosumdb_parts[1], "Go checksum database", build_network
        )

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
    parsed_pip_index = urlparse(pip_index)
    if parsed_pip_index.scheme == "http":
        build_args["PIP_TRUSTED_HOST"] = parsed_pip_index.hostname or ""
    return build_args


def validate_github_mirror_url(value: str, build_network: str) -> str:
    """Validate a GitHub Smart HTTP mirror prefix used only during builds."""
    if not value.strip():
        return ""
    return _validate_source_url(
        value.strip(), "GitHub mirror", build_network
    ).rstrip("/") + "/"


def github_mirror_config_content(github_mirror_url: str) -> str:
    """Render transient system Git config mounted only during Dockerfile RUN."""
    if not github_mirror_url:
        return ""
    lines = [f'[url "{github_mirror_url}"]']
    lines.extend(f"\tinsteadOf = {prefix}" for prefix in GITHUB_GIT_URL_PREFIXES)
    return "\n".join(lines) + "\n"


def optional_package_source_urls(
    args: argparse.Namespace, build_network: str
) -> tuple[str, str]:
    rustup_init = getattr(args, "rustup_init_url", "").strip()
    pytorch_index = getattr(args, "pytorch_index_url", "").strip()
    if rustup_init:
        rustup_init = _validate_source_url(
            rustup_init, "rustup bootstrap", build_network
        )
    if pytorch_index:
        pytorch_index = _validate_source_url(
            pytorch_index, "PyTorch index", build_network
        )
    return rustup_init, pytorch_index


def package_source_hosts(
    build_args: dict[str, str], *additional_urls: str
) -> set[str]:
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
    source: str, *, rustup_init_url: str = "", pytorch_index_url: str = ""
) -> str:
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
            or (path.name != "Dockerfile" and path.suffix not in {".sh", ".bash", ".zsh"})
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


def render_build_dockerfile(
    source: str,
    *,
    dockerhub_mirror_prefix: str,
    apt_mirror: str,
    apt_source_overrides: dict[str, str] | None = None,
    package_build_args: dict[str, str] | None = None,
    rustup_init_url: str = "",
    pytorch_index_url: str = "",
    github_mirror_config_mount_id: str = "",
) -> str:
    package_build_args = package_build_args or {}
    apt_source_overrides = apt_source_overrides or {}
    apt_stages = dockerfile_apt_stages(source)
    source = rewrite_dockerfile_run_source_overrides(
        source,
        apt_source_overrides,
        apt_stages,
    )
    output: list[str] = []
    aliases: set[str] = set()
    stage_index = -1
    stage_mirror_active = False
    current_user_instruction: str | None = None
    active_instruction: str | None = None
    active_apt_source_input = False
    heredocs: list[tuple[str, bool]] = []
    for source_line in source.splitlines():
        if heredocs:
            if (
                active_instruction in {"RUN", "COPY", "ADD"}
                and APT_SOURCE_FILE_REFERENCE.search(source_line)
            ):
                active_apt_source_input = True
            delimiter, strip_tabs = heredocs[0]
            candidate = source_line.lstrip("\t") if strip_tabs else source_line
            if candidate == delimiter:
                output.append(source_line)
                heredocs.pop(0)
                if not heredocs:
                    if active_apt_source_input and stage_mirror_active:
                        append_apt_mirror_refresh(
                            output,
                            apt_mirror,
                            current_user_instruction,
                            apt_source_overrides,
                        )
                    active_instruction = None
                    active_apt_source_input = False
            else:
                output.append(source_line)
            continue

        instruction = None
        if active_instruction is None:
            instruction = DOCKERFILE_INSTRUCTION.match(source_line)
            if instruction:
                active_instruction = instruction.group("name").upper()
                active_apt_source_input = False

        if (
            active_instruction in {"RUN", "COPY", "ADD"}
            and APT_SOURCE_FILE_REFERENCE.search(source_line)
        ):
            active_apt_source_input = True

        line = source_line
        line = rewrite_package_source_urls(
            line,
            rustup_init_url=rustup_init_url,
            pytorch_index_url=pytorch_index_url,
        )
        if active_instruction == "RUN" and github_mirror_config_mount_id:
            line = re.sub(
                r"^(\s*RUN\s+)",
                (
                    r"\1--mount=type=secret,id="
                    f"{github_mirror_config_mount_id},target=/etc/gitconfig,mode=0444,required=true "
                ),
                line,
                count=1,
                flags=re.IGNORECASE,
            )
        match = FROM_LINE.match(line)
        if match:
            if stage_mirror_active:
                append_apt_mirror_cleanup(
                    output,
                    apt_mirror,
                    current_user_instruction,
                    apt_source_overrides,
                )
            stage_index += 1
            stage_mirror_active = False
            current_user_instruction = None
            source_image = match.group("image")
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
            command = apt_mirror_command(
                apt_mirror,
                stage_uses_apt=(
                    stage_index < len(apt_stages) and apt_stages[stage_index]
                ),
                source_overrides=apt_source_overrides,
            )
            if command:
                output.append(command)
                stage_mirror_active = True
        else:
            output.append(line)
            if instruction and active_instruction == "USER":
                current_user_instruction = line

        if active_instruction in {"RUN", "COPY", "ADD"}:
            heredocs.extend(
                (item.group("delimiter"), bool(item.group("strip")))
                for item in HEREDOC_MARKER.finditer(source_line)
            )
        if not heredocs and not source_line.rstrip().endswith("\\"):
            if active_apt_source_input and stage_mirror_active:
                append_apt_mirror_refresh(
                    output,
                    apt_mirror,
                    current_user_instruction,
                    apt_source_overrides,
                )
            active_instruction = None
            active_apt_source_input = False
    if stage_mirror_active:
        append_apt_mirror_cleanup(
            output,
            apt_mirror,
            current_user_instruction,
            apt_source_overrides,
        )
    return "\n".join(output) + "\n"


def parse_build_args(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("build args must be a JSON object")
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not BUILD_ARG_NAME.fullmatch(key):
            raise ValueError(f"invalid build arg name: {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"invalid build arg value for {key!r}")
        result[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def proxy_build_args(
    enabled: bool,
    build_network: str = "default",
    direct_hosts: Iterable[str] = (),
) -> dict[str, str]:
    if not enabled:
        return {}
    configured_proxy = os.environ.get(
        "HARBOR_OPENSANDBOX_BUILD_PROXY_URL", ""
    ).strip()
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    if configured_proxy:
        parsed_proxy = urlparse(configured_proxy)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
            raise ValueError(
                "HARBOR_OPENSANDBOX_BUILD_PROXY_URL must be an http(s) proxy URL"
            )
        result = {
            "HTTP_PROXY": configured_proxy,
            "HTTPS_PROXY": configured_proxy,
            "http_proxy": configured_proxy,
            "https_proxy": configured_proxy,
        }
    else:
        result = {
            name: os.environ[name] for name in proxy_names if os.environ.get(name)
        }
    if not result:
        raise ValueError(
            "--use-proxy requires HARBOR_OPENSANDBOX_BUILD_PROXY_URL or an "
            "HTTP_PROXY/HTTPS_PROXY environment variable"
        )
    for name, value in result.items():
        if (
            urlparse(value).hostname in {"127.0.0.1", "localhost", "::1"}
            and build_network != "host"
        ):
            raise ValueError(
                f"--use-proxy cannot pass loopback proxy {name} into BuildKit "
                "without --build-network=host"
            )
    if "HTTP_PROXY" in result:
        result.setdefault("http_proxy", result["HTTP_PROXY"])
    if "HTTPS_PROXY" in result:
        result.setdefault("https_proxy", result["HTTPS_PROXY"])
    if "http_proxy" in result:
        result.setdefault("HTTP_PROXY", result["http_proxy"])
    if "https_proxy" in result:
        result.setdefault("HTTPS_PROXY", result["https_proxy"])
    for name in ("NO_PROXY", "no_proxy"):
        if os.environ.get(name):
            result[name] = os.environ[name]
    if direct_hosts:
        existing = result.get("NO_PROXY", result.get("no_proxy", ""))
        entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
        for direct_host in sorted(set(direct_hosts)):
            if direct_host not in entries:
                entries.append(direct_host)
        result["NO_PROXY"] = ",".join(entries)
        result["no_proxy"] = result["NO_PROXY"]
    return result


def run_build(
    *,
    environment_dir: Path,
    dockerfile: Path,
    archive_path: Path,
    log_path: Path,
    platform: str,
    timeout_sec: float,
    build_args: dict[str, str],
    target: str | None = None,
    no_cache: bool = False,
    build_network: str = "default",
    secret_files: dict[str, Path] | None = None,
) -> None:
    child_env = os.environ.copy()
    child_env.update(build_args)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def execute(command: list[str], log_handle, operation: str, timeout: float) -> None:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )

        def terminate_process_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                process.wait()
                return
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_group()
            raise RuntimeError(
                f"timed out {operation} after {timeout:g}s; see {log_path}"
            ) from exc
        except BaseException:
            terminate_process_group()
            raise
        if return_code != 0:
            raise RuntimeError(
                f"{operation} failed with exit code {return_code}; see {log_path}"
            )

    buildx_available = subprocess.run(
        ["docker", "buildx", "version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if not buildx_available:
        raise RuntimeError(
            "docker buildx is required to build benchmark task images, but the "
            "plugin is unavailable or broken; install Docker Buildx and verify "
            "`docker buildx version` before retrying"
        )

    with log_path.open("w", encoding="utf-8") as log_handle:
        command = [
            "docker",
            "buildx",
            "build",
            f"--file={dockerfile}",
            f"--platform={platform}",
            f"--output=type=oci,dest={archive_path},compression=gzip,force-compression=true",
            "--provenance=false",
            "--progress=plain",
        ]
        for name in sorted(build_args):
            command.extend(("--build-arg", name))
        for secret_id, secret_path in sorted((secret_files or {}).items()):
            command.extend(("--secret", f"id={secret_id},src={secret_path}"))
        if build_network != "default":
            command.append(f"--network={build_network}")
        if no_cache:
            command.append("--no-cache")
        if target:
            command.append(f"--target={target}")
        command.append(str(environment_dir))
        execute(command, log_handle, "building task image", timeout_sec)


def docker_credentials(config_path: Path, registry: str) -> tuple[str, str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auths = config.get("auths") or {}
    candidates = (registry, f"https://{registry}", f"http://{registry}")
    entry = next((auths[key] for key in candidates if key in auths), None)
    if not entry or not entry.get("auth"):
        raise RuntimeError(
            f"no inline Docker login found for {registry!r} in {config_path}; "
            "run docker login or configure an explicit supported credential source"
        )
    decoded = base64.b64decode(entry["auth"]).decode("utf-8")
    if ":" not in decoded:
        raise RuntimeError("Docker auth entry has an invalid format")
    username, password = decoded.split(":", 1)
    return username, password


def registry_credentials(config_path: Path, registry: str) -> tuple[str, str]:
    """Prefer the ignored Harbor credential environment, then Docker config."""
    username = os.environ.get("YICLOUD_HARBOR_USERNAME", "").strip()
    password = os.environ.get("YICLOUD_HARBOR_PASSWORD", "")
    if username or password:
        if not username or not password:
            raise RuntimeError(
                "YICLOUD_HARBOR_USERNAME and YICLOUD_HARBOR_PASSWORD must be set together"
            )
        return username, password
    return docker_credentials(config_path, registry)


def validate_registry_host(registry: str) -> str:
    host = registry.strip()
    if not host:
        raise ValueError("--registry or YICLOUD_HARBOR_HOST is required")
    if "://" in host or "/" in host or "@" in host or any(
        character.isspace() for character in host
    ):
        raise ValueError(
            "registry must be a bare OCI registry host, "
            f"got: {registry!r}"
        )
    return host


@dataclass(frozen=True)
class RegistryTarget:
    """The immutable address scope for one task's Harbor repository."""

    registry: str
    project: str
    task_repository: str

    @property
    def repository(self) -> str:
        return f"{self.project}/{self.task_repository}"

    def tag(self, service: str, input_hash: str) -> str:
        return f"{safe_tag_component(service)}-{input_hash.removeprefix('sha256:')[:20]}"

    def tag_ref(self, service: str, input_hash: str) -> str:
        return f"{self.registry}/{self.repository}:{self.tag(service, input_hash)}"

    def digest_ref(self, artifact_digest: str) -> str:
        return f"{self.registry}/{self.repository}@{artifact_digest}"


def check_task_repository(task_identity: str, *, maximum_length: int = 255) -> str:
    """Validate that a task identity can be used verbatim as its repository."""
    if not task_identity:
        raise ValueError("task identity must not be empty")
    if len(task_identity) > maximum_length:
        raise ValueError(
            f"task identity exceeds the {maximum_length}-character repository limit: "
            f"{task_identity!r}; fix the dataset adapter instead of renaming it during upload"
        )
    # OCI/Docker repository path components permit one dot or underscore,
    # two underscores, or one-or-more dashes between lowercase alphanumeric
    # runs. In particular, SWE-Rebench's ``owner__repository-issue`` identity
    # is already valid and must remain unchanged.
    if not re.fullmatch(
        r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", task_identity
    ):
        raise ValueError(
            f"task identity is not a valid OCI repository component: {task_identity!r}; "
            "fix the dataset adapter instead of renaming it during upload"
        )
    return task_identity


class SkopeoPublisher:
    """Thin, task-scoped `skopeo` Registry publisher.

    It intentionally delegates blob probing, mounting and upload mechanics to
    skopeo; this class only performs login, copy and independent inspection.
    """

    def __init__(self, target: RegistryTarget, username: str, password: str, *, tls_verify: bool) -> None:
        self.target = target
        self.username = username
        self.password = password
        self.tls_verify = tls_verify
        self._logged_in = False
        # `skopeo login` otherwise writes to the process-wide XDG runtime
        # auth.json. Batch prebuild has several independent publisher
        # processes, which can truncate that shared file while logging in.
        self._auth_dir = tempfile.TemporaryDirectory(
            prefix="opensandbox-skopeo-auth-"
        )
        self._authfile = str(Path(self._auth_dir.name) / "auth.json")

    def close(self) -> None:
        self._auth_dir.cleanup()

    def __del__(self) -> None:
        # Best effort for callers that abort before `prepare_bundle` returns.
        self.close()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment.pop(name, None)
        return environment

    def _run(self, command: list[str], *, input_text: str | None = None) -> str:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            env=self._environment(),
            check=False,
            timeout=1800,
        )
        if completed.returncode:
            error = completed.stderr.strip()[-1000:]
            raise RuntimeError(
                f"skopeo command failed (exit={completed.returncode}): "
                f"{' '.join(command)}; {error or '<no stderr>'}"
            )
        return completed.stdout

    def login(self) -> None:
        if self._logged_in:
            return
        command = [
            "skopeo",
            "login",
            "--authfile",
            self._authfile,
            self.target.registry,
            "--username",
            self.username,
            "--password-stdin",
        ]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        self._run(command, input_text=self.password)
        self._logged_in = True

    def _image_url(self, ref: str) -> str:
        return f"docker://{ref}"

    def inspect(self, ref: str) -> dict[str, str] | None:
        self.login()
        # Skopeo 1.4 (the current YiCloud runner package) exposes Digest but
        # not MediaType in inspect templates. Digest is the required cache and
        # runtime identity; the v2s2 copy format determines the media type.
        command = [
            "skopeo",
            "inspect",
            "--authfile",
            self._authfile,
            "--format",
            "{{.Digest}}",
        ]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        command.append(self._image_url(ref))
        try:
            output = self._run(command).strip().split(maxsplit=1)
        except RuntimeError as exc:
            # Skopeo emits both a 404 and a clear manifest-not-known error for
            # cache misses. Keep real registry/auth errors fatal.
            message = str(exc).lower()
            if (
                (("repository " in message or "artifact " in message) and " not found" in message)
                or any(
                marker in message
                for marker in (
                    "manifest unknown",
                    "repository not found",
                    "name unknown",
                    "status code: 404",
                )
                )
            ):
                return None
            raise
        if not output or not output[0].startswith("sha256:"):
            raise RuntimeError(f"skopeo inspect returned no manifest digest for {ref}")
        return {"artifact_digest": output[0], "media_type": DOCKER_MANIFEST}

    def inspect_config(self, ref: str) -> dict[str, object]:
        """Return the final image config for a published or external image."""
        self.login()
        command = ["skopeo", "inspect", "--authfile", self._authfile, "--config"]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        command.append(self._image_url(ref))
        try:
            payload = json.loads(self._run(command))
        except ValueError as exc:
            raise RuntimeError(f"skopeo inspect --config returned invalid JSON for {ref}") from exc
        return normalize_oci_image_config(payload)

    def copy(self, source: str, destination: str, *, source_is_archive: bool = False) -> dict[str, str]:
        self.login()
        command = [
            "skopeo",
            "copy",
            "--format",
            "v2s2",
            "--src-authfile",
            self._authfile,
            "--dest-authfile",
            self._authfile,
        ]
        command.append("--dest-tls-verify=true" if self.tls_verify else "--dest-tls-verify=false")
        if not source_is_archive:
            command.append("--src-tls-verify=true" if self.tls_verify else "--src-tls-verify=false")
        source_ref = f"oci-archive:{source}" if source_is_archive else self._image_url(source)
        # `--digestfile` captures the manifest digest produced by skopeo's
        # v2s2 conversion. Read it independently from a subsequent inspect so
        # a successful copy cannot silently record a different target tag.
        with tempfile.NamedTemporaryFile(prefix="skopeo-digest-", delete=False) as handle:
            digest_path = Path(handle.name)
        try:
            copy_command = [
                *command,
                "--digestfile",
                str(digest_path),
                source_ref,
                self._image_url(destination),
            ]
            for attempt in range(1, SKOPEO_COPY_ATTEMPTS + 1):
                try:
                    self._run(copy_command)
                    break
                except RuntimeError as exc:
                    if attempt == SKOPEO_COPY_ATTEMPTS:
                        raise
                    log(
                        "warning: skopeo copy failed "
                        f"(attempt {attempt}/{SKOPEO_COPY_ATTEMPTS}); "
                        f"retrying in {SKOPEO_COPY_RETRY_DELAY_SECONDS}s: {exc}"
                    )
                    time.sleep(SKOPEO_COPY_RETRY_DELAY_SECONDS)
            copied_digest = digest_path.read_text(encoding="utf-8").strip()
        finally:
            digest_path.unlink(missing_ok=True)
        inspected = self.inspect(destination)
        if inspected is None:
            raise RuntimeError(f"skopeo copy succeeded but target tag cannot be inspected: {destination}")
        if copied_digest != inspected["artifact_digest"]:
            raise RuntimeError(
                "skopeo copy/inspect digest mismatch for "
                f"{destination}: copy={copied_digest!r} inspect={inspected['artifact_digest']!r}"
            )
        return inspected


class RegistryClient:
    """Task-scoped read client; publishing is intentionally delegated to skopeo."""

    def __init__(self, target: RegistryTarget, publisher: SkopeoPublisher) -> None:
        self.target = target
        self.publisher = publisher

    def manifest(self, tag: str) -> dict[str, str] | None:
        return self.publisher.inspect(f"{self.target.registry}/{self.target.repository}:{tag}")


def blob_member_name(digest: str) -> str:
    algorithm, value = digest.split(":", 1)
    if algorithm != "sha256":
        raise RuntimeError(f"unsupported digest algorithm: {algorithm}")
    return f"blobs/sha256/{value}"


def read_member_bytes(
    archive: tarfile.TarFile, name: str, *, max_bytes: int = 16 * 1024 * 1024
) -> bytes:
    member_info = archive.getmember(name)
    if member_info.size > max_bytes:
        raise RuntimeError(f"OCI metadata member is unexpectedly large: {name}")
    member = archive.extractfile(member_info)
    if member is None:
        raise RuntimeError(f"OCI archive is missing {name}")
    return member.read()


def schema2_manifest(
    archive: tarfile.TarFile,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    index = json.loads(read_member_bytes(archive, "index.json"))
    source_descriptors = index.get("manifests") or []
    if len(source_descriptors) != 1:
        raise RuntimeError(
            "expected exactly one platform manifest, "
            f"found {len(source_descriptors)}"
        )
    source_descriptor = source_descriptors[0]
    source_bytes = read_member_bytes(
        archive, blob_member_name(str(source_descriptor["digest"]))
    )
    if digest_bytes(source_bytes) != source_descriptor["digest"]:
        raise RuntimeError("OCI source manifest digest mismatch")
    source_manifest = json.loads(source_bytes)

    config = dict(source_manifest["config"])
    if config.get("mediaType") not in {OCI_CONFIG, DOCKER_CONFIG}:
        raise RuntimeError(f"unsupported config media type: {config.get('mediaType')}")
    config["mediaType"] = DOCKER_CONFIG
    layers: list[dict[str, object]] = []
    for source_layer in source_manifest.get("layers") or []:
        layer = dict(source_layer)
        if layer.get("mediaType") not in {OCI_LAYER_GZIP, DOCKER_LAYER_GZIP}:
            raise RuntimeError(
                f"cannot map layer media type to Docker schema2: {layer.get('mediaType')}"
            )
        layer["mediaType"] = DOCKER_LAYER_GZIP
        layers.append(layer)
    return (
        {
            "schemaVersion": 2,
            "mediaType": DOCKER_MANIFEST,
            "config": config,
            "layers": layers,
        },
        [config, *layers],
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def image_identity(environment_dir: Path, *, docker_image: str | None = None) -> str:
    """Return the Harbor benchmark framework's static environment identity."""
    return environment_content_hash(
        environment_dir,
        docker_image=docker_image,
        truncate=64,
    )


def build_image_identity(environment_dir: Path) -> str:
    """Namespace a task build hash by the generated-Dockerfile contract."""
    task_identity = image_identity(environment_dir)
    payload = (
        f"opensandbox-build-renderer\0{BUILD_RENDERER_VERSION}\0{task_identity}"
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PreparedBundle:
    main_image_ref: str
    manifest: dict[str, object]
    manifest_path: Path | None


def _path_relative_to_environment(path: Path, environment_dir: Path) -> str:
    try:
        return path.relative_to(environment_dir).as_posix()
    except ValueError as exc:
        raise ValueError(f"build path escapes task environment: {path}") from exc


def _string_argv(value: object, *, label: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"OCI image config {label} must be a string list or null")
    return list(value)


def _oci_port_entries(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise TypeError("OCI image config ExposedPorts must be an object or null")
    ports: list[dict[str, object]] = []
    for token in sorted(raw):
        if not isinstance(token, str):
            raise TypeError("OCI image config ExposedPorts keys must be strings")
        raw_port, separator, raw_protocol = token.partition("/")
        protocol = raw_protocol.lower() if separator else "tcp"
        if not raw_port.isdigit() or protocol not in {"tcp", "udp"}:
            raise RuntimeError(f"invalid OCI image config exposed port: {token!r}")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise RuntimeError(f"invalid OCI image config exposed port: {token!r}")
        ports.append({"port": port, "protocol": protocol})
    return ports


def normalize_oci_image_config(raw: object) -> dict[str, object]:
    """Extract only OCI fields needed by the Bundle runtime contract."""
    if not isinstance(raw, dict):
        raise TypeError("OCI image config must be a JSON object")
    config = raw.get("config", raw)
    if not isinstance(config, dict):
        raise TypeError("OCI image config .config must be a JSON object")
    healthcheck = config.get("Healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise RuntimeError("OCI image config Healthcheck must be an object or null")
    normalized_healthcheck = dict(healthcheck) if healthcheck is not None else None
    if normalized_healthcheck is not None and "Test" in normalized_healthcheck:
        normalized_healthcheck["test"] = normalized_healthcheck.pop("Test")
    working_dir = config.get("WorkingDir")
    if working_dir is not None and not isinstance(working_dir, str):
        raise RuntimeError("OCI image config WorkingDir must be a string or null")
    working_dir = working_dir or None
    if working_dir is not None and not working_dir.startswith("/"):
        raise RuntimeError("OCI image config WorkingDir must be an absolute path")
    return {
        "entrypoint": _string_argv(config.get("Entrypoint"), label="Entrypoint"),
        "cmd": _string_argv(config.get("Cmd"), label="Cmd"),
        "working_dir": working_dir,
        "exposed_ports": _oci_port_entries(config.get("ExposedPorts")),
        "healthcheck": normalized_healthcheck,
    }


def oci_archive_image_config(archive_path: Path) -> dict[str, object]:
    """Read the final image config from a local OCI archive before publishing."""
    with tarfile.open(archive_path, "r") as archive:
        index = json.loads(read_member_bytes(archive, "index.json"))
        descriptors = index.get("manifests") or []
        if len(descriptors) != 1:
            raise RuntimeError(
                "expected exactly one platform manifest while reading OCI image config, "
                f"found {len(descriptors)}"
            )
        descriptor = descriptors[0]
        manifest_bytes = read_member_bytes(
            archive, blob_member_name(str(descriptor["digest"]))
        )
        if digest_bytes(manifest_bytes) != descriptor["digest"]:
            raise RuntimeError("OCI image manifest digest mismatch")
        manifest = json.loads(manifest_bytes)
        config = manifest.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
            raise TypeError("OCI image manifest has no config descriptor")
        config_bytes = read_member_bytes(
            archive, blob_member_name(config["digest"])
        )
        if digest_bytes(config_bytes) != config["digest"]:
            raise RuntimeError("OCI image config digest mismatch")
    return normalize_oci_image_config(json.loads(config_bytes))


def _compose_argv(value: object, *, label: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise RuntimeError(f"invalid Compose {label}: {exc}") from exc
    raise RuntimeError(f"Compose {label} must be a string or string list")


def _compose_runtime(
    service: ServiceSpec,
    image_config: dict[str, object],
    *,
    benchmark: str,
    task_identity: str,
    image_config_resolved: bool = True,
    legacy_dockerfile_keepalive: bool = False,
) -> dict[str, object]:
    """Materialize OCI defaults and Compose overrides for one provider run."""
    image_entrypoint = image_config.get("entrypoint")
    image_command = image_config.get("cmd")
    image_working_dir = image_config.get("working_dir")
    if image_entrypoint is not None and not isinstance(image_entrypoint, list):
        raise RuntimeError("normalized OCI image entrypoint is invalid")
    if image_command is not None and not isinstance(image_command, list):
        raise RuntimeError("normalized OCI image command is invalid")
    if image_working_dir is not None and not isinstance(image_working_dir, str):
        raise RuntimeError("normalized OCI image working directory is invalid")

    entrypoint_overridden = service.entrypoint_present and service.entrypoint is not None
    if entrypoint_overridden:
        effective_entrypoint = _compose_argv(service.entrypoint, label="entrypoint")
        entrypoint_source = "compose.entrypoint"
    else:
        effective_entrypoint = list(image_entrypoint or [])
        entrypoint_source = "image-config.entrypoint" if image_entrypoint else None

    command_overridden = service.command_present and service.command is not None
    if legacy_dockerfile_keepalive:
        # Harbor's Docker backend overlays every implicit single-Dockerfile
        # task with ``command: [sh, -c, sleep infinity]``. Mirror that
        # contract instead of releasing the image's default Cmd as a service
        # process: language base images commonly default to an interactive
        # interpreter (for example ``python3``), which exits immediately when
        # detached from stdin.
        effective_command = list(LEGACY_DOCKERFILE_KEEPALIVE)
        command_source = "adapter.legacy-keepalive"
    elif command_overridden:
        effective_command = _compose_argv(service.command, label="command")
        command_source = "compose.command"
    elif entrypoint_overridden:
        # Compose entrypoint override suppresses the image Cmd unless Compose
        # also explicitly supplies command.
        effective_command = []
        command_source = None
    else:
        effective_command = list(image_command or [])
        command_source = "image-config.cmd" if image_command else None

    start_argv = [*effective_entrypoint, *effective_command]
    sources = [source for source in (entrypoint_source, command_source) if source]
    start_source = "+".join(sources) if sources else None

    if service.working_dir is not None:
        workdir = service.working_dir
        workdir_source = "compose.working_dir"
    else:
        workdir = image_working_dir
        workdir_source = "image-config.working-dir" if workdir else None

    ports: list[dict[str, object]] = []
    seen_ports: set[tuple[int, str]] = set()

    def add_port(port: int, protocol: str, source: str) -> None:
        key = (port, protocol)
        if key not in seen_ports:
            seen_ports.add(key)
            ports.append({"port": port, "protocol": protocol, "source": source})

    for item in service.ports:
        raw_port = item.get("target") if isinstance(item, dict) else item
        raw_text = str(raw_port).split("/", 1)[0].rsplit(":", 1)[-1]
        protocol = (
            str(item.get("protocol", "tcp")).lower()
            if isinstance(item, dict)
            else (str(raw_port).split("/", 1)[1].lower() if "/" in str(raw_port) else "tcp")
        )
        if raw_text.isdigit() and protocol in {"tcp", "udp"} and 1 <= int(raw_text) <= 65535:
            add_port(int(raw_text), protocol, "compose.ports.target")
    for item in service.expose:
        raw_text, separator, raw_protocol = str(item).partition("/")
        protocol = raw_protocol.lower() if separator else "tcp"
        if raw_text.isdigit() and protocol in {"tcp", "udp"} and 1 <= int(raw_text) <= 65535:
            add_port(int(raw_text), protocol, "compose.expose")
        else:
            raise RuntimeError(f"invalid Compose expose port for service {service.name!r}: {item!r}")
    for item in image_config.get("exposed_ports") or []:
        if not isinstance(item, dict):
            raise TypeError("normalized OCI image exposed ports are invalid")
        add_port(int(item["port"]), str(item["protocol"]), "image-config.exposed-ports")

    healthcheck = service.healthcheck or image_config.get("healthcheck")
    healthcheck_source = (
        "compose.healthcheck" if service.healthcheck else "image-config.healthcheck"
    ) if healthcheck else None
    readiness: dict[str, object] | None = None
    if healthcheck:
        readiness = {"type": "healthcheck", "healthcheck": healthcheck, "source": healthcheck_source}
    else:
        metadata = OPENSANDBOX_ADAPTER_METADATA.get(benchmark, {}).get(task_identity, {}).get(service.name, {})
        candidate = metadata.get("readiness")
        if isinstance(candidate, dict):
            port = candidate.get("port")
            if candidate.get("type") == "tcp" and isinstance(port, int) and any(
                entry["port"] == port and entry["protocol"] == "tcp" for entry in ports
            ):
                readiness = {
                    "type": "tcp",
                    "port": port,
                    "source": f"adapter-metadata:{benchmark}/{task_identity}/{service.name}",
                }
            elif image_config_resolved:
                raise RuntimeError(
                    f"OpenSandbox adapter readiness metadata has no matching TCP internal port for {service.name!r}"
                )

    return {
        "start_argv": start_argv,
        "start_argv_source": start_source,
        "workdir": workdir,
        "workdir_source": workdir_source,
        "internal_ports": ports,
        "readiness": readiness,
    }


def inspect_external_image(
    image_ref: str,
    dockerhub_mirror_prefix: str,
    platform: str,
    *,
    dry_run: bool,
) -> tuple[str, str]:
    resolved_ref = mirror_image_ref(
        image_ref, dockerhub_mirror_prefix, aliases=set()
    )
    if dry_run:
        payload = f"dry-run\0{resolved_ref}\0{platform}".encode()
        return resolved_ref, digest_bytes(payload)
    completed = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", resolved_ref],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0 or not completed.stdout:
        error = completed.stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise RuntimeError(
            f"failed to inspect external image {resolved_ref!r}: "
            f"exit={completed.returncode} error={error or '<none>'}"
        )
    return resolved_ref, digest_bytes(completed.stdout)


def _transport_record(
    *,
    dockerhub_mirror_prefix: str,
    apt_mirror: str,
    apt_source_overrides: dict[str, str],
    package_build_args: dict[str, str],
    github_mirror_url: str,
    rustup_init_url: str,
    pytorch_index_url: str,
) -> dict[str, str]:
    return {
        "apt_mirror": apt_mirror,
        "apt_source_override_origins": ",".join(sorted(apt_source_overrides)),
        "dockerhub_mirror_prefix": dockerhub_mirror_prefix,
        "github_mirror_configured": str(bool(github_mirror_url)).lower(),
        "package_source_args": ",".join(sorted(package_build_args)),
        "pytorch_index_configured": str(bool(pytorch_index_url)).lower(),
        "rustup_init_configured": str(bool(rustup_init_url)).lower(),
    }


def _read_record(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def local_upload_manifest_path(
    cache_root: Path,
    target: RegistryTarget,
    *,
    benchmark: str,
    platform: str,
) -> Path:
    """Return the persistent, target-scoped uploaded-Bundle index entry."""
    scope = json.dumps(
        {
            "benchmark": benchmark,
            "platform": platform,
            "project": target.project,
            "registry": target.registry,
            "version": LOCAL_UPLOAD_INDEX_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    scope_key = hashlib.sha256(scope).hexdigest()[:20]
    return (
        cache_root
        / "uploaded-bundles"
        / scope_key
        / f"{target.task_repository}.json"
    )


def _cached_bundle_matches_target(
    manifest: dict[str, object],
    *,
    target: RegistryTarget,
    benchmark: str,
    task_identity: str,
    platform: str,
) -> bool:
    registry = manifest.get("registry")
    benchmark_record = manifest.get("benchmark")
    services = manifest.get("services")
    main = manifest.get("main")
    if (
        manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or manifest.get("bundle_format") != BUNDLE_FORMAT_VERSION
        or manifest.get("task_identity") != task_identity
        or not isinstance(registry, dict)
        or registry.get("host") != target.registry
        or registry.get("project") != target.project
        or registry.get("task_repository") != target.task_repository
        or registry.get("repository") != target.repository
        or not isinstance(benchmark_record, dict)
        or benchmark_record.get("name") != benchmark
        or not isinstance(services, dict)
        or not services
        or not isinstance(main, str)
        or main not in services
    ):
        return False

    for service_record in services.values():
        if not isinstance(service_record, dict):
            return False
        image = service_record.get("image")
        if not isinstance(image, dict) or image.get("platform") != platform:
            return False
        artifact_digest = image.get("artifact_digest")
        input_hash = image.get("input_hash")
        if (
            not isinstance(artifact_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest)
            or image.get("digest_ref") != target.digest_ref(artifact_digest)
            or not isinstance(input_hash, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", input_hash)
        ):
            return False
    return True


def _cached_bundle_matches_content(
    manifest: dict[str, object], bundle: BundleSpec
) -> bool:
    if manifest.get("definition_identity") != f"sha256:{bundle.definition_identity}":
        return False
    services = manifest.get("services")
    if not isinstance(services, dict) or set(services) != set(bundle.services):
        return False
    for name, service in bundle.services.items():
        service_record = services.get(name)
        if not isinstance(service_record, dict):
            return False
        image = service_record.get("image")
        if not isinstance(image, dict):
            return False
        if service.build is not None:
            expected_hash = "sha256:" + build_image_identity(
                bundle.environment_dir
            )
        else:
            expected_hash = "sha256:" + image_identity(
                bundle.environment_dir,
                docker_image=service.source_image,
            )
        if image.get("input_hash") != expected_hash:
            return False
    return True


def _prepared_from_cached_bundle(
    manifest: dict[str, object],
    *,
    cached_path: Path,
    configured_output: Path | None,
) -> PreparedBundle:
    manifest_path = cached_path
    if configured_output is not None:
        manifest_path = Path(configured_output).expanduser().resolve()
        atomic_write_json(manifest_path, manifest)
    services = manifest.get("services")
    main = manifest.get("main")
    if not isinstance(services, dict) or not isinstance(main, str):
        raise TypeError(f"invalid local uploaded-Bundle entry: {cached_path}")
    main_record = services.get(main)
    image = main_record.get("image") if isinstance(main_record, dict) else None
    if not isinstance(image, dict):
        raise TypeError(f"invalid local uploaded-Bundle main service: {cached_path}")
    return PreparedBundle(
        main_image_ref=str(image["digest_ref"]),
        manifest=manifest,
        manifest_path=manifest_path,
    )


def _service_image_inputs(
    service: ServiceSpec,
    *,
    bundle: BundleSpec,
    dockerhub_mirror_prefix: str,
    package_build_args: dict[str, str],
    platform: str,
    explicit_build_args: dict[str, str],
    dry_run: bool,
) -> tuple[
    str,
    dict[str, str],
    dict[str, str],
    str | None,
    str | None,
]:
    if service.build is not None:
        # Mirror routes are defaults. A task/Compose build arg may select an
        # explicit package source, and the operator's explicit JSON override
        # remains authoritative over both.
        effective_build_args = {
            **package_build_args,
            **service.build.args,
            **explicit_build_args,
        }
        declared_build_args = {
            **service.build.args,
            **explicit_build_args,
        }
        # Dynamic build args and source/proxy values remain deployment
        # adaptations, but a renderer contract change must never reuse an
        # image produced before that contract existed.
        identity = build_image_identity(bundle.environment_dir)
        return identity, declared_build_args, effective_build_args, None, None
    if not service.source_image:
        raise ValueError(f"service {service.name!r} has no build or source image")
    resolved_ref, source_digest = inspect_external_image(
        service.source_image,
        dockerhub_mirror_prefix,
        platform,
        dry_run=dry_run,
    )
    identity = image_identity(
        bundle.environment_dir,
        docker_image=service.source_image,
    )
    return identity, {}, {}, resolved_ref, source_digest


def _prepare_service_image(
    *,
    service: ServiceSpec,
    bundle: BundleSpec,
    args: argparse.Namespace,
    explicit_build_args: dict[str, str],
    proxy_args: dict[str, str],
    cache_root: Path,
    target: RegistryTarget,
    publisher: SkopeoPublisher | None,
    registry: RegistryClient | None,
) -> dict[str, object]:
    (
        identity,
        declared_build_args,
        effective_service_build_args,
        resolved_external_ref,
        source_digest,
    ) = _service_image_inputs(
        service,
        bundle=bundle,
        dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
        package_build_args=args.package_build_args,
        platform=args.platform,
        explicit_build_args=explicit_build_args,
        dry_run=args.dry_run,
    )
    input_hash = f"sha256:{identity}"
    tag = target.tag(service.name, input_hash)
    tag_ref = target.tag_ref(service.name, input_hash)
    image_source = "build" if service.build is not None else "external-mirror"
    artifact: dict[str, object] = {
        "source": image_source,
        "input_hash": input_hash,
        "tag": tag,
        "tag_ref": tag_ref,
        "artifact_digest": None,
        "digest_ref": None,
        "media_type": DOCKER_MANIFEST,
        "platform": args.platform,
        "build_arg_names": sorted(declared_build_args),
        "config": {
            "entrypoint": None,
            "cmd": None,
            "exposed_ports": [],
            "healthcheck": None,
        },
        "config_resolved": False,
    }
    if source_digest is not None:
        artifact["source_manifest_digest"] = source_digest
    if args.dry_run:
        artifact["artifact_digest"] = digest_bytes(
            f"dry-run\0{identity}".encode()
        )
        artifact["digest_ref"] = target.digest_ref(str(artifact["artifact_digest"]))
        return artifact

    if publisher is None:
        raise RuntimeError("Skopeo publisher is unavailable outside dry-run mode")
    target_key = hashlib.sha256(target.repository.encode("utf-8")).hexdigest()[:16]
    lock_path = cache_root / "locks" / "images" / f"{target_key}-{tag}.lock"
    record_path = cache_root / "records" / "images" / target_key / f"{tag}.json"
    log_path = cache_root / "logs" / bundle.task_identity / f"{tag}.log"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        inspected = None if args.force else registry.manifest(tag) if registry else None
        if inspected is not None:
            log(f"registry cache hit service={service.name}: {tag_ref}")
            existing_record = _read_record(record_path)
            artifact["artifact_digest"] = inspected["artifact_digest"]
            artifact["digest_ref"] = target.digest_ref(inspected["artifact_digest"])
            artifact["media_type"] = inspected["media_type"]
            artifact["config"] = publisher.inspect_config(tag_ref)
            artifact["config_resolved"] = True
            atomic_write_json(
                record_path,
                {
                    **existing_record,
                    "input_hash": input_hash,
                    "platform": args.platform,
                    "tag": tag,
                    "tag_ref": tag_ref,
                    "artifact_digest": inspected["artifact_digest"],
                    "digest_ref": artifact["digest_ref"],
                    "source": existing_record.get("source", image_source),
                    "last_resolution": "registry-cache",
                    "transport": _transport_record(
                        dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                        apt_mirror=args.apt_mirror,
                        apt_source_overrides=args.apt_source_overrides,
                        package_build_args=args.package_build_args,
                        github_mirror_url=args.github_mirror_url,
                        rustup_init_url=args.rustup_init_url,
                        pytorch_index_url=args.pytorch_index_url,
                    ),
                    "build_arg_names": sorted(declared_build_args),
                    "service": service.name,
                    "task_dir": str(bundle.task_dir),
                    "last_resolved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return artifact

        local_image_config: dict[str, object] | None = None
        cache_root.mkdir(parents=True, exist_ok=True)
        if service.build is None:
            if not resolved_external_ref:
                raise RuntimeError("external service resolved without an image ref")
            inspected = publisher.copy(resolved_external_ref, tag_ref)
        else:
            with tempfile.TemporaryDirectory(prefix=f"{tag}-", dir=cache_root) as temporary_dir:
                temporary = Path(temporary_dir)
                build_context, rewritten_scripts = materialize_package_source_context(
                    service.build.context_dir,
                    temporary / "context",
                    rustup_init_url=args.rustup_init_url,
                    pytorch_index_url=args.pytorch_index_url,
                )
                if rewritten_scripts:
                    log(
                        "package source configuration rewrote an exact origin URL in "
                        f"{len(rewritten_scripts)} build script(s)"
                    )
                dockerfile_source = service.build.dockerfile.read_text(encoding="utf-8")
                target_stage = service.build.target
                rendered_dockerfile = temporary / "Dockerfile"
                github_mirror_config = github_mirror_config_content(
                    args.github_mirror_url
                )
                github_mirror_config_path: Path | None = None
                if github_mirror_config:
                    github_mirror_config_path = temporary / "gitconfig"
                    github_mirror_config_path.write_text(
                        github_mirror_config, encoding="utf-8"
                    )
                rendered_dockerfile.write_text(
                    render_build_dockerfile(
                        dockerfile_source,
                        dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                        apt_mirror=args.apt_mirror,
                        apt_source_overrides=args.apt_source_overrides,
                        package_build_args=args.package_build_args,
                        rustup_init_url=args.rustup_init_url,
                        pytorch_index_url=args.pytorch_index_url,
                        github_mirror_config_mount_id=(
                            GITHUB_MIRROR_CONFIG_MOUNT_ID if github_mirror_config else ""
                        ),
                    ),
                    encoding="utf-8",
                )
                archive_path = temporary / "image.oci.tar"
                effective_build_args = {
                    **proxy_args,
                    **effective_service_build_args,
                }
                build_timeout = getattr(args, "build_timeout_sec", None) or load_build_timeout(bundle.task_dir)
                log(f"building task={bundle.task_identity} service={service.name} platform={args.platform}; log={log_path}")
                run_build(
                    environment_dir=build_context,
                    dockerfile=rendered_dockerfile,
                    archive_path=archive_path,
                    log_path=log_path,
                    platform=args.platform,
                    timeout_sec=build_timeout,
                    build_args=effective_build_args,
                    target=target_stage,
                    no_cache=getattr(args, "no_cache", False),
                    build_network=getattr(args, "build_network", "default"),
                    secret_files=(
                        {GITHUB_MIRROR_CONFIG_MOUNT_ID: github_mirror_config_path}
                        if github_mirror_config_path is not None
                        else None
                    ),
                )
                local_image_config = oci_archive_image_config(archive_path)
                log(f"publishing service={service.name}: {tag_ref}")
                inspected = publisher.copy(str(archive_path), tag_ref, source_is_archive=True)
        artifact["artifact_digest"] = inspected["artifact_digest"]
        artifact["digest_ref"] = target.digest_ref(inspected["artifact_digest"])
        artifact["media_type"] = inspected["media_type"]
        resolution = "built-and-pushed"
        artifact["config"] = local_image_config or publisher.inspect_config(tag_ref)
        artifact["config_resolved"] = True
        atomic_write_json(
            record_path,
            {
                "build_log": str(log_path),
                "input_hash": input_hash,
                "tag": tag,
                "tag_ref": tag_ref,
                "artifact_digest": artifact["artifact_digest"],
                "digest_ref": artifact["digest_ref"],
                "media_type": artifact["media_type"],
                "platform": args.platform,
                "proxy_configured": bool(proxy_args),
                "source": image_source,
                "source_manifest_digest": source_digest,
                "last_resolution": resolution,
                "transport": _transport_record(
                    dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                    apt_mirror=args.apt_mirror,
                    apt_source_overrides=args.apt_source_overrides,
                    package_build_args=args.package_build_args,
                    github_mirror_url=args.github_mirror_url,
                    rustup_init_url=args.rustup_init_url,
                    pytorch_index_url=args.pytorch_index_url,
                ),
                "build_arg_names": sorted(declared_build_args),
                "service": service.name,
                "task_dir": str(bundle.task_dir),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    log(f"ready service={service.name}: {artifact['digest_ref']}")
    return artifact


def _service_manifest(
    service: ServiceSpec,
    artifact: dict[str, object],
    environment_dir: Path,
    *,
    benchmark: str,
    task_identity: str,
    definition_kind: str,
) -> dict[str, object]:
    build: dict[str, object] | None = None
    if service.build is not None:
        build = {
            "context": _path_relative_to_environment(
                service.build.context_dir, environment_dir
            ),
            "dockerfile": _path_relative_to_environment(
                service.build.dockerfile, environment_dir
            ),
            "target": service.build.target,
            # Values may be credentials. The immutable manifest exposes only
            # names; runtime overrides never participate in image identity.
            "build_arg_names": artifact["build_arg_names"],
        }
    image_config = artifact.get("config")
    if not isinstance(image_config, dict):
        raise TypeError(f"service {service.name!r} has no normalized OCI image config")
    return {
        "image": artifact,
        "build": build,
        "source_image": service.source_image,
        "entrypoint": service.entrypoint,
        "entrypoint_present": service.entrypoint_present,
        "command": service.command,
        "command_present": service.command_present,
        "working_dir": service.working_dir,
        "environment": service.environment,
        "ports": service.ports,
        "expose": service.expose,
        "aliases": service.aliases,
        "depends_on": service.depends_on,
        "healthcheck": service.healthcheck,
        "volumes": service.volumes,
        "networks": service.networks,
        "cap_add": service.cap_add,
        "privileged": service.privileged,
        "container_name": service.container_name,
        "resources": service.resources,
        "unsupported_fields": service.unsupported_fields,
        "runtime": _compose_runtime(
            service,
            image_config,
            benchmark=benchmark,
            task_identity=task_identity,
            image_config_resolved=bool(artifact.get("config_resolved", True)),
            legacy_dockerfile_keepalive=definition_kind == "dockerfile",
        ),
    }


def _bundle_identity(
    bundle: BundleSpec, services: dict[str, dict[str, object]]
) -> str:
    # Registry location/tag/schema are materialization details. A copied
    # Bundle with identical artifacts and topology retains its identity.
    images = {
        name: services[name]["image"]["artifact_digest"]
        for name in sorted(services)
    }
    payload = {
        "main_service": bundle.main_service,
        "images": images,
        "topology": {
            name: {
                key: services[name].get(key)
                for key in (
                    "entrypoint", "entrypoint_present", "command", "command_present",
                    "working_dir",
                    "environment", "ports", "expose", "aliases",
                    "depends_on", "healthcheck", "volumes", "networks", "cap_add",
                    "privileged", "resources", "unsupported_fields", "runtime",
                )
            }
            for name in sorted(services)
        },
    }
    return digest_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def prepare_bundle(args: argparse.Namespace) -> PreparedBundle:
    # Platform deliberately stays out of the content hash. If a real task ever
    # requires another architecture, isolate its images and local artifacts in
    # an architecture-specific Harbor Project/namespace before relaxing this
    # guard; never reuse or overwrite the default amd64 cache with --force.
    args.platform = getattr(args, "platform", DEFAULT_PLATFORM)
    if args.platform != DEFAULT_PLATFORM:
        raise NotImplementedError(
            f"OpenSandbox task-image platform {args.platform!r} is not implemented; "
            f"only {DEFAULT_PLATFORM!r} is currently supported"
        )
    args.registry = validate_registry_host(args.registry)
    task_dir = resolve_task_dir(args.task_dir, args.dataset_root, args.include)
    cache_root = args.cache_root.resolve()
    benchmark = args.benchmark_name or args.project
    target = RegistryTarget(
        registry=args.registry,
        project=args.project,
        task_repository=args.task_repository
        or check_task_repository(
            task_dir.name,
            maximum_length=255 - len(args.project) - 1,
        ),
    )
    reuse_local_upload = bool(getattr(args, "reuse_local_upload_cache", False))
    skip_hash_verification = bool(getattr(args, "skip_hash_verification", False))
    upload_manifest_path = local_upload_manifest_path(
        cache_root,
        target,
        benchmark=benchmark,
        platform=args.platform,
    )
    cached_manifest = (
        _read_record(upload_manifest_path)
        if reuse_local_upload and not args.force and not args.dry_run
        else {}
    )
    cached_target_matches = bool(cached_manifest) and _cached_bundle_matches_target(
        cached_manifest,
        target=target,
        benchmark=benchmark,
        task_identity=task_dir.name,
        platform=args.platform,
    )
    configured_output = getattr(args, "bundle_manifest_output", None)
    if cached_target_matches and skip_hash_verification:
        log(
            "local uploaded-Bundle cache hit "
            f"task={task_dir.name} verification=skipped: {upload_manifest_path}"
        )
        return _prepared_from_cached_bundle(
            cached_manifest,
            cached_path=upload_manifest_path,
            configured_output=configured_output,
        )

    bundle = resolve_bundle_spec(task_dir)
    if cached_target_matches and _cached_bundle_matches_content(cached_manifest, bundle):
        log(
            "local uploaded-Bundle cache hit "
            f"task={task_dir.name} verification=content-hash: {upload_manifest_path}"
        )
        return _prepared_from_cached_bundle(
            cached_manifest,
            cached_path=upload_manifest_path,
            configured_output=configured_output,
        )
    if cached_manifest and reuse_local_upload and not args.force and not args.dry_run:
        log(
            "local uploaded-Bundle cache miss "
            f"task={task_dir.name}; falling back to Registry resolution"
        )
    build_network = getattr(args, "build_network", "default")
    args.apt_mirror = validate_apt_mirror(
        getattr(args, "apt_mirror", DEFAULT_APT_MIRROR),
        build_network,
    )
    args.apt_source_overrides = parse_apt_source_overrides(
        getattr(args, "apt_source_overrides_json", "{}"),
        build_network,
    )
    args.github_mirror_url = validate_github_mirror_url(
        getattr(args, "github_mirror_url", ""), build_network
    )
    args.package_build_args = package_source_build_args(args, build_network)
    args.rustup_init_url, args.pytorch_index_url = optional_package_source_urls(
        args, build_network
    )
    explicit_build_args = parse_build_args(args.build_args_json)
    publisher: SkopeoPublisher | None = None
    registry: RegistryClient | None = None
    proxy_args: dict[str, str] = {}
    if not args.dry_run:
        username, password = registry_credentials(args.docker_config, args.registry)
        publisher = SkopeoPublisher(
            target,
            username,
            password,
            tls_verify=args.registry_tls_verify,
        )
        registry = RegistryClient(target, publisher)
        proxy_args = proxy_build_args(
            args.use_proxy,
            build_network,
            direct_hosts=package_source_hosts(
                args.package_build_args,
                args.apt_mirror,
                args.github_mirror_url,
                args.rustup_init_url,
                args.pytorch_index_url,
                *args.apt_source_overrides.values(),
            ),
        )

    artifacts: dict[str, dict[str, object]] = {}
    for name in sorted(bundle.services):
        artifacts[name] = _prepare_service_image(
            service=bundle.services[name],
            bundle=bundle,
            args=args,
            explicit_build_args=explicit_build_args,
            proxy_args=proxy_args,
            cache_root=cache_root,
            target=target,
            publisher=publisher,
            registry=registry,
        )

    services = {
        name: _service_manifest(
            bundle.services[name],
            artifacts[name],
            bundle.environment_dir,
            benchmark=benchmark,
            task_identity=bundle.task_identity,
            definition_kind=bundle.definition_kind,
        )
        for name in sorted(bundle.services)
    }
    bundle_identity = _bundle_identity(bundle, services)
    manifest: dict[str, object] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_format": BUNDLE_FORMAT_VERSION,
        "benchmark": {"name": benchmark},
        "task_identity": bundle.task_identity,
        "registry": {
            "host": target.registry,
            "project": target.project,
            "task_repository": target.task_repository,
            "repository": target.repository,
        },
        "definition_kind": bundle.definition_kind,
        "definition_identity": f"sha256:{bundle.definition_identity}",
        "bundle_identity": bundle_identity,
        "normalization_backend": bundle.normalization_backend,
        "main": bundle.main_service,
        "services": services,
        "requirements": bundle.requirements,
    }

    if reuse_local_upload and not args.dry_run:
        atomic_write_json(upload_manifest_path, manifest)
    manifest_path: Path | None = None
    if configured_output is not None:
        manifest_path = Path(configured_output).expanduser().resolve()
        atomic_write_json(manifest_path, manifest)
    elif not args.dry_run:
        manifest_path = (
            cache_root
            / "bundles"
            / f"{bundle_identity.removeprefix('sha256:')}.json"
        )
        atomic_write_json(manifest_path, manifest)

    main_image_ref = str(services[bundle.main_service]["image"]["digest_ref"])
    prepared = PreparedBundle(
        main_image_ref=main_image_ref,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    if publisher is not None:
        publisher.close()
    return prepared


def prepare(args: argparse.Namespace) -> str:
    """Backward-compatible single string result used by existing callers."""
    return prepare_bundle(args).main_image_ref


def default_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else fallback


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare content-addressed YiCloud OpenSandbox service images and "
            "an immutable environment bundle"
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task-dir", type=Path)
    source.add_argument("--dataset-root", type=Path)
    parser.add_argument("--include", default=os.environ.get("INCLUDE_TASKS", ""))
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
        default=os.environ.get("HARBOR_OPENSANDBOX_BENCHMARK", ""),
        help="source benchmark name recorded in the Bundle",
    )
    # Compatibility-only migration input. It is deliberately not used as a
    # target repository: split project/repository forms are rejected unless
    # the project can be unambiguously derived.
    parser.add_argument("--repository", default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_REPOSITORY", ""), help=argparse.SUPPRESS)
    parser.add_argument("--sandbox-image-prefix", default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--docker-config",
        type=Path,
        default=default_path(
            "HARBOR_OPENSANDBOX_DOCKER_CONFIG", Path.home() / ".docker" / "config.json"
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=default_path(
            "HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT",
            Path("/data/harbor-runs/opensandbox-images"),
        ),
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_PLATFORM", DEFAULT_PLATFORM),
        help=f"target image platform; currently only {DEFAULT_PLATFORM} is implemented",
    )
    parser.add_argument(
        "--tag-prefix",
        default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX", "harbor"),
    )
    parser.add_argument(
        "--dockerhub-mirror-prefix",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX", "m.daocloud.io/docker.io"
        ),
    )
    parser.add_argument(
        "--apt-mirror",
        default=os.environ.get("HARBOR_OPENSANDBOX_APT_MIRROR", DEFAULT_APT_MIRROR),
        help="APT mirror root containing ubuntu, debian, and debian-security",
    )
    parser.add_argument(
        "--apt-source-overrides-json",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON", "{}"
        ),
        help=(
            "JSON object mapping third-party APT URL prefixes to explicit "
            "build-time source URL prefixes"
        ),
    )
    parser.add_argument(
        "--pip-index-url",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_PIP_INDEX_URL", DEFAULT_PIP_INDEX_URL
        ),
    )
    parser.add_argument(
        "--npm-registry",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_NPM_REGISTRY", DEFAULT_NPM_REGISTRY
        ),
    )
    parser.add_argument(
        "--goproxy",
        default=os.environ.get("HARBOR_OPENSANDBOX_GOPROXY", DEFAULT_GOPROXY),
    )
    parser.add_argument(
        "--gosumdb",
        default=os.environ.get("HARBOR_OPENSANDBOX_GOSUMDB", DEFAULT_GOSUMDB),
    )
    parser.add_argument(
        "--cargo-registry-url",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL",
            DEFAULT_CARGO_REGISTRY_URL,
        ),
    )
    parser.add_argument(
        "--rustup-dist-server",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER",
            DEFAULT_RUSTUP_DIST_SERVER,
        ),
    )
    parser.add_argument(
        "--rustup-update-root",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT",
            DEFAULT_RUSTUP_UPDATE_ROOT,
        ),
    )
    parser.add_argument(
        "--github-mirror-url",
        default=os.environ.get("HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL", ""),
        help=(
            "optional build-only GitHub Smart HTTP mirror prefix; applies to "
            "clone, fetch, and recursive submodules"
        ),
    )
    parser.add_argument(
        "--rustup-init-url",
        default=os.environ.get("HARBOR_OPENSANDBOX_RUSTUP_INIT_URL", ""),
        help="optional trusted replacement for exact sh.rustup.rs bootstrap URLs",
    )
    parser.add_argument(
        "--pytorch-index-url",
        default=os.environ.get("HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL", ""),
        help="optional trusted replacement for exact download.pytorch.org/whl URLs",
    )
    parser.add_argument(
        "--package-source-timeout-sec",
        type=int,
        default=int(
            os.environ.get("HARBOR_OPENSANDBOX_PACKAGE_SOURCE_TIMEOUT_SEC", "300")
        ),
    )
    parser.add_argument(
        "--build-args-json",
        default=os.environ.get("HARBOR_OPENSANDBOX_BUILD_ARGS_JSON", "{}"),
    )
    parser.add_argument(
        "--registry-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("YICLOUD_HARBOR_TLS_VERIFY", "0") in {"1", "true", "TRUE"},
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
        "--retry-no-cache-on-apt-404",
        action="store_true",
        help="retry once without cache when a cached apt index fetches a missing package",
    )
    parser.add_argument(
        "--build-network",
        choices=("default", "host"),
        default=os.environ.get("HARBOR_OPENSANDBOX_BUILD_NETWORK", "host"),
        help="network mode for Dockerfile RUN instructions",
    )
    parser.add_argument(
        "--use-proxy",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("HARBOR_OPENSANDBOX_BUILD_USE_PROXY", "1")
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
        parser.error(
            "--skip-hash-verification requires --reuse-local-upload-cache"
        )
    return args


def main() -> int:
    args = parse_args()
    prepared = prepare_bundle(args)
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
