"""Internal, SDK-free handoff between Harbor Shell and backend launch policy.

Run with python3 -S to avoid activating runtime hooks during preflight. Stdout
contains fixed Shell assignments/array appends with shlex-quoted values only;
diagnostics go to stderr, and failed operations emit no Shell program.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from harbor_environment_adapters.core.config import configured_backend_plan
from harbor_environment_adapters.core.launch import (
    environment_args,
    launch_capabilities,
    validate_backend,
    validate_verifier_transport,
)


def assignments(values: dict[str, str]) -> str:
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())


def image_assignments(images) -> str:
    return assignments({
        "HARBOR_OPENSANDBOX_IMAGE_REF": images.image_ref,
        "HARBOR_OPENSANDBOX_BUNDLE_MANIFEST": images.bundle_manifest,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--ready", action="store_true")
    commands.add_parser("args")
    commands.add_parser("verifier-transport")
    image = commands.add_parser("image-plan")
    image.add_argument("manifest")
    prepared = commands.add_parser("image-result")
    prepared.add_argument("image_ref")
    prepared.add_argument("manifest")
    args = parser.parse_args()
    config = os.environ
    messages = []
    output = ""
    try:
        plan = configured_backend_plan(config)
        caps = launch_capabilities(plan)
        if args.command == "validate":
            messages = validate_backend(plan, config, ready=args.ready)
            if caps.task_images:
                from task_image_manager.launch import (
                    configured_images,
                    validate_image_preparation,
                )

                images = configured_images(config)
                validate_image_preparation(config, images)
                output = image_assignments(images)
        elif args.command == "args":
            values = environment_args(plan, config, Path(__file__).resolve().parent)
            output = f"cmd+=({shlex.join(values)})"
            if config.get("HARBOR_DRY_RUN") == "1" and not caps.task_images:
                messages.append("Docker environment; add unprivileged Docker Compose overlay"
                                if caps.host_docker else
                                f"{plan.environment_type} environment; skip unprivileged Docker Compose overlay")
        elif args.command == "verifier-transport":
            validate_verifier_transport(plan, config)
        elif args.command == "image-plan":
            output = "manager_args=()"
            if caps.task_images:
                from task_image_manager.launch import image_preparation

                images, messages = image_preparation(config, args.manifest)
                output = image_assignments(images)
                output += f"\nmanager_args=({shlex.join(images.manager_args)})"
        elif args.command == "image-result":
            from task_image_manager.launch import prepared_images

            output = image_assignments(prepared_images(args.image_ref, args.manifest))
    except ValueError as error:
        for line in str(error).splitlines():
            print(f"[ERROR] {line}", file=sys.stderr)
        return 1
    except OSError:
        print("[ERROR] failed to read local backend launch inputs", file=sys.stderr)
        return 1
    for message in messages:
        print(f"[INFO] {message}", file=sys.stderr)
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
