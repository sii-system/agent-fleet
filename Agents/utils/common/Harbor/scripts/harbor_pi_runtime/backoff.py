"""Shared exponential backoff for Harbor Pi agent retries."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable

DEFAULT_INITIAL_SECONDS = 1.0
DEFAULT_MAX_SECONDS = 30.0


def _non_negative_finite_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def retry_delay_seconds(completed_attempt: int) -> float:
    """Return the delay before the attempt following ``completed_attempt``."""

    if completed_attempt < 1:
        raise ValueError("completed_attempt must be positive")
    initial = _non_negative_finite_env(
        "HARBOR_AGENT_RETRY_INITIAL_SECONDS", DEFAULT_INITIAL_SECONDS
    )
    maximum = _non_negative_finite_env(
        "HARBOR_AGENT_RETRY_MAX_SECONDS", DEFAULT_MAX_SECONDS
    )
    return min(initial * (2 ** (completed_attempt - 1)), maximum)


def sleep_before_retry(
    completed_attempt: int,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> float:
    """Sleep with bounded exponential backoff and return the chosen delay."""

    delay = retry_delay_seconds(completed_attempt)
    if delay > 0:
        sleeper(delay)
    return delay
