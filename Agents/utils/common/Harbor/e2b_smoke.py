"""Validate native Harbor E2B canary results and check sandbox cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


class CanaryError(ValueError):
    """A canary failure with a credential-free diagnostic."""


def check_results(root: Path) -> list[str]:
    trials = []
    for path in root.rglob("result.json"):
        result = json.loads(path.read_text(encoding="utf-8"))
        if "trial_name" in result:
            trials.append((path, result))
    if len(trials) != 1:
        raise CanaryError(f"expected one trial result, found {len(trials)}")
    path, result = trials[0]
    environment = result.get("config", {}).get("environment", {})
    if environment.get("type") != "e2b" or environment.get("import_path"):
        raise CanaryError("canary did not use Harbor's native E2B environment")
    if result.get("exception_info") or not result.get("finished_at"):
        raise CanaryError("canary failed or did not finish; inspect the trial result")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if rewards.get("reward") != 1:
        raise CanaryError("canary verifier reward is not 1")
    artifact = path.parent / "artifacts" / "e2b-smoke.txt"
    if not artifact.is_file() or artifact.read_text(encoding="utf-8") != "NATIVE-E2B\n":
        raise CanaryError("canary artifact missing or incorrect")
    # Harbor 0.18.0 uses this session ID for the task's agent environment.
    return [f"{result['trial_name']}__env"]


async def check_cleanup(sessions, list_sandboxes) -> None:
    from e2b import SandboxQuery

    for session in sessions:
        pager = list_sandboxes(
            query=SandboxQuery(metadata={"session_id": session}), request_timeout=30
        )
        remaining = 0
        while pager.has_next:
            remaining += len(await pager.next_items())
        if remaining:
            raise CanaryError(f"canary cleanup incomplete: {remaining} sandbox(s) remain")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--check-cleanup", action="store_true",
                        help="query the configured E2B API for this trial's sandboxes")
    args = parser.parse_args(argv)
    try:
        sessions = check_results(args.results)
        if args.check_cleanup:
            from e2b import AsyncSandbox

            asyncio.run(check_cleanup(sessions, AsyncSandbox.list))
    except CanaryError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - redact provider errors at the CLI boundary
        # SDK errors may include request details; leave credentials out of output.
        print(f"[ERROR] canary validation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("[OK] native E2B trial: reward=1, artifact verified")
    if args.check_cleanup:
        print("[OK] no running or paused sandboxes remain for this trial")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
