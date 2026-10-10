"""Preserve ALE's native scalar score and detailed evaluation artifacts."""

import json
import math

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from .environment import ALEEnvironment


def score(result):
    if result.get("error"):
        raise RuntimeError("Native ALE evaluator failed; see native-result.json")
    for key in ("score", "final_score"):
        value = result.get(key)
        if value is not None:
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("ALE reward must be finite")
            return value
    raise ValueError("ALE evaluator did not return a numeric score")


class ALEVerifier(BaseVerifier):
    async def verify(self):
        if not isinstance(self.environment, ALEEnvironment):
            raise TypeError("ALEVerifier requires the agent's ALE environment")
        result = await self.environment.native_phase(
            "evaluate", verifier_env={**(self.verifier_env or {}), **self.override_env},
        )
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.verifier_dir / "native-result.json").write_text(json.dumps(result, indent=2) + "\n")
        reward = score(result)
        self.trial_paths.reward_text_path.write_text(str(reward) + "\n")
        return VerifierResult(rewards={"reward": reward})
