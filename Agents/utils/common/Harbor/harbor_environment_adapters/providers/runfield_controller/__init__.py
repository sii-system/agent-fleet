"""Export the Harbor environment adapter for Runfield Controller."""

from typing import Any

__all__ = ["RunfieldSandboxEnvironment"]


def __getattr__(name: str) -> Any:
    """Keep SDK-free startup preflight imports independent of Harbor."""

    if name != "RunfieldSandboxEnvironment":
        raise AttributeError(name)
    from .adapter import RunfieldSandboxEnvironment

    return RunfieldSandboxEnvironment
