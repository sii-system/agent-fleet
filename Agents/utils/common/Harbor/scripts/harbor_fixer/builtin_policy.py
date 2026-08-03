"""Built-in T1 allow and deny rules for Harbor Fixer execution policy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_DENY_EXECUTABLES = {
    "rm",
    "rmdir",
    "shred",
    "unlink",
    "wipefs",
}
_READ_ONLY_COMMANDS = {
    "cat",
    "diff",
    "du",
    "echo",
    "file",
    "grep",
    "head",
    "id",
    "ls",
    "printf",
    "pwd",
    "rg",
    "stat",
    "tail",
    "test",
    "true",
    "uname",
    "wc",
    "which",
}
_SAFE_GIT_SUBCOMMANDS = {
    "diff",
    "log",
    "rev-parse",
    "show",
    "status",
}
_SAFE_DOCKER_SUBCOMMANDS = {
    "images",
    "info",
    "inspect",
    "logs",
    "ps",
    "version",
}
_FIND_WRITE_ACTIONS = {
    "-delete",
    "-exec",
    "-execdir",
    "-fprint",
    "-fprint0",
    "-fprintf",
    "-ok",
    "-okdir",
}


def _has_output_option(tokens: Sequence[str]) -> bool:
    return any(token == "--output" or token.startswith("--output=") for token in tokens)


def builtin_destructive_reason(tokens: Sequence[str]) -> tuple[str, str] | None:
    """Return the built-in denial reason for one token sequence, if any."""

    for token in tokens:
        executable = Path(token).name
        if executable in _DENY_EXECUTABLES or executable.startswith("mkfs"):
            return "builtin_destructive_command", executable
    if tokens:
        executable = Path(tokens[0]).name
        if executable == "find" and any(
            token in _FIND_WRITE_ACTIONS for token in tokens[1:]
        ):
            return "builtin_destructive_find", "find"
    return None


def builtin_read_only_reason(
    command_tokens: Sequence[str],
) -> tuple[str, str] | None:
    """Return the built-in allow reason for one normalized command."""

    if not command_tokens:
        return None
    executable = Path(command_tokens[0]).name
    if executable in _READ_ONLY_COMMANDS:
        if executable == "diff" and _has_output_option(command_tokens[1:]):
            return None
        return (
            f"builtin_read_only_{executable}",
            f"built-in read-only command: {executable}",
        )
    if executable == "git" and len(command_tokens) >= 2:
        if _has_output_option(command_tokens[2:]):
            return None
        if command_tokens[1] in _SAFE_GIT_SUBCOMMANDS:
            return (
                "builtin_read_only_git",
                f"built-in read-only git {command_tokens[1]}",
            )
        if list(command_tokens[1:3]) == ["branch", "--show-current"]:
            return "builtin_read_only_git", "built-in read-only git branch query"
    if (
        executable == "docker"
        and len(command_tokens) >= 2
        and command_tokens[1] in _SAFE_DOCKER_SUBCOMMANDS
    ):
        return (
            "builtin_read_only_docker",
            f"built-in read-only docker {command_tokens[1]}",
        )
    if executable == "find" and not any(
        token in _FIND_WRITE_ACTIONS for token in command_tokens[1:]
    ):
        return "builtin_read_only_find", "built-in read-only find"
    return None
