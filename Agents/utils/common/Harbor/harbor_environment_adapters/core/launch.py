"""Host launch policy for the selected BackendPlan; no SDK or remote calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import BackendPlan


class LaunchError(ValueError):
    """A launch configuration cannot satisfy the existing backend contract."""


@dataclass(frozen=True)
class LaunchCapabilities:
    host_docker: bool = False
    readonly_mounts: bool = False
    verifier_upload: bool = False
    task_images: bool = False
    remote_runtime: bool = False
    preinstalled_agents: bool = False


def launch_capabilities(plan: BackendPlan) -> LaunchCapabilities:
    backend = plan.environment_type
    return LaunchCapabilities(
        host_docker=backend == "docker",
        readonly_mounts=backend in {"docker", "opensandbox", "dsec"},
        verifier_upload=backend in {"e2b", "qz", "dsec"},
        task_images=backend in {"opensandbox", "dsec"},
        remote_runtime=backend in {"e2b", "qz", "opensandbox", "dsec"},
        preinstalled_agents=backend == "dsec",
    )


def launch_settings(plan: BackendPlan, config: Mapping[str, str]) -> dict[str, str]:
    """Project launch capabilities and the historical qz source defaults."""
    caps = launch_capabilities(plan)
    if config.get("HARBOR_CC_WEB_MCP_ENABLED") == "1" and caps.verifier_upload:
        raise LaunchError(
            f"Web MCP is not supported on {plan.environment_type}: "
            "artifact delivery requires docker or opensandbox."
        )
    npm = config.get("NPM_CONFIG_REGISTRY") or (
        "https://registry.npmmirror.com" if plan.environment_type == "qz"
        else "https://registry.npmjs.org"
    )
    node = config.get("HARBOR_CC_NODE_DIST_URL", "")
    if plan.environment_type == "qz":
        node = config.get("QZ_NODE_DIST_URL") or node or (
            "https://registry.npmmirror.com/-/binary/node/"
            "v22.14.0/node-v22.14.0-linux-x64.tar.gz"
        )
    return {
        "HARBOR_BACKEND_DISPLAY_NAME": "E2B" if plan.environment_type == "e2b" else plan.environment_type,
        "HARBOR_BACKEND_HOST_DOCKER": str(int(caps.host_docker)),
        "HARBOR_BACKEND_READONLY_MOUNTS": str(int(caps.readonly_mounts)),
        "HARBOR_BACKEND_VERIFIER_UPLOAD": str(int(caps.verifier_upload)),
        "HARBOR_BACKEND_TASK_IMAGES": str(int(caps.task_images)),
        "HARBOR_BACKEND_REMOTE_RUNTIME": str(int(caps.remote_runtime)),
        "HARBOR_BACKEND_PREINSTALLED_AGENTS": str(int(caps.preinstalled_agents)),
        "NPM_CONFIG_REGISTRY": npm,
        "HARBOR_CC_NODE_DIST_URL": node,
    }


def validate_backend(
    plan: BackendPlan, config: Mapping[str, str], *, ready: bool = False,
) -> list[str]:
    """Keep dry-run validation separate from live prerequisite checks."""
    backend = plan.environment_type
    agent = config.get("AGENT", "")
    messages = []
    if backend not in {"docker", "e2b", "opensandbox", "qz", "dsec"}:
        raise LaunchError(
            "HARBOR_ENVIRONMENT_TYPE must be docker, e2b, opensandbox, qz, or dsec, "
            f"got: {backend}"
        )
    if backend in {"e2b", "qz"} and agent in {"pi", "dsh-sdk-minimal"}:
        detail = (
            "Pi's pinned Node/runtime archives and local extensions require host bind mounts."
            if agent == "pi" else "its pinned runtime requires host bind mounts."
        )
        raise LaunchError(
            f"AGENT={agent} with HARBOR_ENVIRONMENT_TYPE={backend} is unsupported: {detail}\n"
            f"use HARBOR_ENVIRONMENT_TYPE=docker or opensandbox for AGENT={agent}."
        )
    if backend == "opensandbox":
        for name in (
            "YICLOUD_PUBLIC_KEY", "YICLOUD_SECRET_KEY", "YICLOUD_PROJECT_NAME",
            "YICLOUD_HARBOR_HOST",
        ):
            if not config.get(name):
                raise LaunchError(f"required environment variable is unset: {name}")
        if not (config.get("YICLOUD_SANDBOX_ENVIRONMENT_ID")
                or config.get("YICLOUD_SANDBOX_ENVIRONMENT_NAME")):
            raise LaunchError(
                "OpenSandbox requires YICLOUD_SANDBOX_ENVIRONMENT_ID or "
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME\n"
                "refusing to create an instance without an explicit environment binding"
            )
        messages.append(
            "OpenSandbox environment binding: "
            f"id={config.get('YICLOUD_SANDBOX_ENVIRONMENT_ID') or '<resolved-by-name>'} "
            f"name={config.get('YICLOUD_SANDBOX_ENVIRONMENT_NAME') or '<lookup-by-id>'}"
        )
    if backend == "dsec":
        endpoint = config.get("DSEC_SANDBOX_CONTROLLER_ENDPOINT", "").strip()
        if not endpoint:
            raise LaunchError(
                "required environment variable is unset: "
                "DSEC_SANDBOX_CONTROLLER_ENDPOINT"
            )
        if endpoint.startswith("http://") and config.get(
            "DSEC_SANDBOX_ALLOW_INSECURE_HTTP", "0"
        ).strip().lower() not in {"1", "true", "yes"}:
            raise LaunchError(
                "plain HTTP DSec Controller requires "
                "DSEC_SANDBOX_ALLOW_INSECURE_HTTP=1"
            )
        messages.append(f"DSec Sandbox Controller: {endpoint}")
    if backend == "qz":
        if not (config.get("SBX_API_KEY") or config.get("QZ_SANDBOX_API_KEY")
                or config.get("E2B_API_KEY", "").startswith("sbx_")):
            raise LaunchError(
                "qz sandbox requires SBX_API_KEY (or QZ_SANDBOX_API_KEY / an sbx_-prefixed E2B_API_KEY)"
            )
        template = config.get("QZ_SANDBOX_TEMPLATE", "")
        mapping = config.get("QZ_SANDBOX_TEMPLATE_MAP", "")
        if template and mapping:
            raise LaunchError("set only one of QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP")
        if not (template or mapping):
            raise LaunchError("qz sandbox requires QZ_SANDBOX_TEMPLATE or QZ_SANDBOX_TEMPLATE_MAP")
        if mapping and not Path(mapping).is_file():
            raise LaunchError(f"QZ_SANDBOX_TEMPLATE_MAP not found: {mapping}")
        timeout = config.get("QZ_SANDBOX_TIMEOUT_SEC", "")
        if timeout and (not re.fullmatch(r"[0-9]+", timeout) or not 1 <= int(timeout) <= 14400):
            raise LaunchError(
                "QZ_SANDBOX_TIMEOUT_SEC must be an integer between 1 and 14400 "
                f"(the platform's 4-hour cap), got: {timeout}"
            )
        if config.get("HARBOR_FORCE_BUILD", "0") not in {"0", "false", "no", ""}:
            raise LaunchError(
                "HARBOR_FORCE_BUILD is not supported on qz: templates are registered "
                "on the platform, not built by Harbor"
            )
        messages.append(f"qz sandbox fixed template: {template}" if template
                        else f"qz sandbox template map: {mapping}")
        if agent in {"claude-code", "opencode"}:
            messages.append(
                f"qz {agent} delivery: npm registry {config.get('NPM_CONFIG_REGISTRY') or '<unset>'} "
                f"| node dist {config.get('HARBOR_CC_NODE_DIST_URL') or '<unset>'}"
            )
    timeout = config.get("HARBOR_E2B_SANDBOX_TIMEOUT_SEC", "")
    if timeout and not re.fullmatch(r"[1-9][0-9]*", timeout):
        raise LaunchError("HARBOR_E2B_SANDBOX_TIMEOUT_SEC must be a positive integer")
    if ready:
        if backend == "e2b" and not config.get("E2B_API_KEY"):
            raise LaunchError("E2B_API_KEY is required when HARBOR_ENVIRONMENT_TYPE=e2b")
        if backend in {"e2b", "qz"}:
            name = "E2B" if backend == "e2b" else "qz sandbox"
            messages.append(f"using {name} environment; skip host Docker daemon and Docker Hub preflight")
    return messages


def environment_args(plan: BackendPlan, config: Mapping[str, str], harbor_dir: Path) -> list[str]:
    args = ["--env", plan.environment_spec]
    caps = launch_capabilities(plan)
    if caps.task_images:
        for key, name in (
            ("image_ref", "HARBOR_OPENSANDBOX_IMAGE_REF"),
            ("bundle_manifest_path", "HARBOR_OPENSANDBOX_BUNDLE_MANIFEST"),
            (
                "ttl_seconds"
                if plan.environment_type == "dsec"
                else "lifecycle_minutes",
                "DSEC_SANDBOX_TTL_SECONDS"
                if plan.environment_type == "dsec"
                else "YICLOUD_SANDBOX_LIFECYCLE_MINUTES",
            ),
        ):
            args += ["--ek", f"{key}={config.get(name, '')}"]
    elif caps.host_docker:
        args += ["--extra-docker-compose", str(harbor_dir / "overlays/unprivileged-task.yaml")]
    return args


def validate_verifier_transport(plan: BackendPlan, config: Mapping[str, str]) -> None:
    bundle = config.get("VERIFIER_RUNTIME_BUNDLE_ID", "none")
    if (bundle != "none" and plan.environment_type == "opensandbox"
            and config.get("YICLOUD_SANDBOX_UPLOAD_BACKEND") not in {"s3", "auto"}):
        raise LaunchError(
            f"verifier runtime bundle {bundle} requires YICLOUD_SANDBOX_UPLOAD_BACKEND=s3 or auto"
        )
