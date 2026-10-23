"""Project a shared BackendPlan into legacy Shell variables.

Run with python3 -S: configuration resolution must not trigger provider startup
hooks against the listener's previous environment. No SDK imports are needed.
"""

import argparse
import os
import shlex
import sys

from harbor_environment_adapters.core.config import (
    PLAN_ENV,
    configured_backend_plan,
    rollout_backend_plan,
)
from harbor_environment_adapters.core.launch import launch_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout-environment-type")
    args = parser.parse_args()
    try:
        plan = (
            rollout_backend_plan(os.environ, args.rollout_environment_type)
            if args.rollout_environment_type is not None
            else configured_backend_plan(os.environ)
        )
        settings = launch_settings(plan, os.environ)
    except ValueError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    # Keys are fixed and values are Shell-quoted, including custom import paths.
    for name, value in {
        "HARBOR_ENVIRONMENT_TYPE": plan.environment_type,
        "HARBOR_ENVIRONMENT_SPEC": plan.environment_spec,
        PLAN_ENV: plan.to_json(),
        **settings,
    }.items():
        print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
