"""Resolve backend selection without importing SDKs or modifying the environment.

Callers pass their already merged configuration (local files and caller
overrides). Empty values preserve the legacy shell's `:-` fallback semantics.
The plan contains selection only; authentication stays with each provider.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass

PLAN_ENV = "HARBOR_BACKEND_PLAN"


@dataclass(frozen=True)
class BackendPlan:
    """Immutable selection, retaining the exact Harbor import path."""

    environment_type: str
    environment_spec: str

    def to_json(self) -> str:
        return json.dumps({"schema": 1, **asdict(self)}, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "BackendPlan":
        """Read only the selection fields, without echoing invalid input."""
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid HARBOR_BACKEND_PLAN JSON") from error
        fields = {"environment_type", "environment_spec"}
        if (
            not isinstance(data, dict)
            or set(data) != fields | {"schema"}
            or type(data.get("schema")) is not int
            or data["schema"] != 1
            or any(not isinstance(data[key], str) or not data[key] for key in fields)
        ):
            raise ValueError("invalid HARBOR_BACKEND_PLAN schema")
        return cls(**{key: data[key] for key in fields})


def resolve_backend_plan(config: Mapping[str, str]) -> BackendPlan:
    """Mirror env/environment_config.sh selection, including custom backends.

    An explicit spec is opaque: it may replace any built-in implementation.
    No credentials or unrelated configuration are retained in the plan.
    """
    backend = (
        config.get("HARBOR_ENVIRONMENT_TYPE")
        or config.get("RL_ENVIRONMENT_TYPE")
        or "docker"
    )
    spec = config.get("HARBOR_ENVIRONMENT_SPEC")
    if not spec:
        if backend == "opensandbox":
            spec = "yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
        elif backend == "dsec":
            spec = "dsec_sandbox:DsecSandboxEnvironment"
        elif backend == "qz":
            spec = "qz_e2b_sandbox:QzSandboxEnvironment"
        elif backend == "e2b" and config.get("HARBOR_E2B_PREBUILT_TEMPLATE"):
            spec = (
                config.get("HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC")
                or "e2b_prebuilt:PrebuiltE2BEnvironment"
            )
        else:
            spec = backend
    return BackendPlan(backend, spec)


def configured_backend_plan(config: Mapping[str, str]) -> BackendPlan:
    """Reuse an inherited selection unless the merged legacy inputs changed.

    env.sh is sourced again by workers and harboropik.sh. Preserve the selected
    plan across those loads while allowing explicit legacy input overrides.
    """
    value = config.get(PLAN_ENV)
    if value:
        plan = BackendPlan.from_json(value)
        backend = (
            config.get("HARBOR_ENVIRONMENT_TYPE")
            or config.get("RL_ENVIRONMENT_TYPE")
            or "docker"
        )
        if (
            backend == plan.environment_type
            and config.get("HARBOR_ENVIRONMENT_SPEC") == plan.environment_spec
        ):
            return plan
    return resolve_backend_plan(config)


def rollout_backend_plan(
    config: Mapping[str, str], environment_type: str
) -> BackendPlan:
    """Preserve the legacy request override policy without inheriting a stale spec.

    qz/OpenSandbox and prebuilt E2B select their adapter per request. Docker and
    ordinary E2B retain a host custom spec only for the listener's RL default.
    Request payloads cannot supply import paths or provider credentials.
    """
    request_config = dict(config)
    request_config["HARBOR_ENVIRONMENT_TYPE"] = environment_type
    if (
        environment_type in {"opensandbox", "qz", "dsec"}
        or (environment_type == "e2b" and config.get("HARBOR_E2B_PREBUILT_TEMPLATE"))
        or environment_type != (config.get("RL_ENVIRONMENT_TYPE") or "docker")
    ):
        request_config.pop("HARBOR_ENVIRONMENT_SPEC", None)
    return resolve_backend_plan(request_config)


def runtime_backend_plan(config: Mapping[str, str]) -> BackendPlan:
    """Consume the launcher's plan, with the old direct-Python fallback.

    A direct Python launch historically defaults to Docker, ignoring RL settings.
    Startup hooks retain their case/whitespace normalization after selection.
    """
    if config.get(PLAN_ENV):
        return BackendPlan.from_json(config[PLAN_ENV])
    return resolve_backend_plan(
        {
            **config,
            "HARBOR_ENVIRONMENT_TYPE": config.get("HARBOR_ENVIRONMENT_TYPE")
            or "docker",
        }
    )
