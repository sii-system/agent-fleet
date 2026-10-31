"""Stable compatibility import path for the DSec Harbor environment."""

from harbor_environment_adapters.providers.dsec_controller.adapter import (
    DsecSandboxEnvironment,
)

__all__ = ["DsecSandboxEnvironment"]
