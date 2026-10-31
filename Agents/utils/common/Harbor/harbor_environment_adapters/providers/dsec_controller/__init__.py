"""Harbor environment adapter for the DSec Sandbox Controller."""

from typing import Any

__all__ = ["DsecSandboxEnvironment"]


def __getattr__(name: str) -> Any:
    """Keep SDK-free startup preflight imports independent of Harbor."""

    if name != "DsecSandboxEnvironment":
        raise AttributeError(name)
    from .adapter import DsecSandboxEnvironment

    return DsecSandboxEnvironment
