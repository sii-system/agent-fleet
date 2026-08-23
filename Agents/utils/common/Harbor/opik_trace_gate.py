#!/usr/bin/env python3
from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _is_true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _TRUE_VALUES)


def opik_tracing_enabled(
    extra_env: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    if extra_env is not None and "OPIK_URL" in extra_env:
        opik_url = extra_env["OPIK_URL"]
    else:
        opik_url = values.get("OPIK_URL", "")

    disabled = _is_true(values.get("OPIK_TRACK_DISABLE"))
    if extra_env is not None:
        disabled = disabled or _is_true(extra_env.get("OPIK_TRACK_DISABLE"))
    return bool(opik_url.strip()) and not disabled


if __name__ == "__main__":
    raise SystemExit(0 if opik_tracing_enabled() else 1)
