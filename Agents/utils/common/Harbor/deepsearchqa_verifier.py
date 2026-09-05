from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harbor.verifier.verifier import Verifier

_JUDGE_ENV_NAMES = ("JUDGE_BASE_URL", "JUDGE_API_KEY", "JUDGE_MODEL")
_OVERLAY_DIR = Path(__file__).with_name("deepsearchqa_verifier_files")


class DeepSearchQAVerifier(Verifier):
    def __init__(
        self,
        *args: Any,
        verifier_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        judge_env = {name: os.environ.get(name, "") for name in _JUDGE_ENV_NAMES}
        super().__init__(
            *args,
            verifier_env={**(verifier_env or {}), **judge_env},
            **kwargs,
        )

    def _resolve_tests(self) -> tuple[list[Path], Path, Path]:
        source_dirs, _, _ = super()._resolve_tests()
        test_path = _OVERLAY_DIR / "test.sh"
        return [*source_dirs, _OVERLAY_DIR], _OVERLAY_DIR, test_path
