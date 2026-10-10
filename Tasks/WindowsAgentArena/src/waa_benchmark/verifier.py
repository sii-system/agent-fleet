"""Harbor verifier preserving the selected release's native reward semantics."""

from __future__ import annotations

import json
import math

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier


class WAAVerifier(BaseVerifier):
    async def verify(self):
        from .environment import WAASession

        if not isinstance(self.environment, WAASession):
            raise TypeError("WAAVerifier requires a WAA session attached to the agent's VM")
        result = await self.environment.native_request("evaluate")
        score = float(result["score"])
        if not math.isfinite(score):
            raise ValueError("Native WAA evaluator returned a nonfinite reward")
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        self.trial_paths.reward_text_path.write_text(str(score) + "\n")
        (self.trial_paths.verifier_dir / "native-result.json").write_text(json.dumps(result, indent=2) + "\n")
        return VerifierResult(rewards={"reward": score})
