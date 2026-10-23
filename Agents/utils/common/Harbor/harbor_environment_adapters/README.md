# Harbor environment adapters

This package owns backend selection and the existing YiCloud/qz runtime
implementations. The old modules retain the Harbor import paths as compatibility
imports of the new classes.

`core/config.py` contains the immutable `BackendPlan` recovered and adapted from
`archive/harbor-environment-adapters-phase1`. It records the environment type,
exact Harbor spec. It does not retain credentials
or other runtime configuration, import SDKs, or create remote resources.

## Configuration handoff

1. `env.sh` merges public, local, and caller configuration using the existing
   config loader, then applies rollout/template defaults.
2. `env/environment_config.sh` calls `backend_plan.py` with `python3 -S` so the
   configuration helper cannot activate provider startup hooks prematurely.
3. The helper exports `HARBOR_BACKEND_PLAN` (schema 1 JSON), together with
   `HARBOR_ENVIRONMENT_TYPE` and `HARBOR_ENVIRONMENT_SPEC` for existing consumers.
   `core/launch.py` also projects internal `HARBOR_BACKEND_*` launch flags and
   the historical qz npm/Node defaults. Flags are recomputed from the plan on
   every resolution, including request overrides; caller-supplied flags do not
   choose a backend.
4. Fixed and registry command builders and backend preflight use those projected
   variables. Re-sourcing `env.sh` reuses the plan while type/spec are unchanged.
   Changes to those legacy inputs produce a new plan.
5. The legacy rollout worker resolves a plan inside each request's subshell.
   Its historical spec override policy is preserved: qz/OpenSandbox and prebuilt
   E2B use their configured adapters; changing backend clears the listener spec;
   a request for the listener's default Docker or ordinary E2B retains a custom
   host spec. Request payloads cannot set import paths or credentials.
6. Shared `sitecustomize.py` consumes the serialized plan. Direct Python callers
   without a plan retain the old Docker default and hook normalization.
   Native rollout consumes the already prepared Harbor TrialConfig, retaining
   its existing Claude Code/OpenSandbox restriction.

`HARBOR_BACKEND_PLAN` is an internal process handoff, not a new configuration
input users need to set. Old variables and `module:Class` specs remain supported.
qz authentication, host patches, templates, verifier injection, and runtime
delivery retain their contracts. `backend_launch.py` delegates preflight checks
and environment CLI arguments to `core/launch.py`; SDK imports and remote
operations stay out of this launch policy. Read-only artifact delivery, verifier
uploads, Docker requirements, and remote runtime source selection are derived
once, rather than maintained as separate Shell backend lists. These are launch
decisions; Harbor and provider runtime capabilities remain authoritative.

Image selection belongs to `task_image_manager/launch.py`. Explicit Bundle/image
inputs retain precedence over task.toml images and automatic local preparation;
native OpenSandbox trials retain their per-task prebuilt-image restriction.
The Shell calls the existing image CLI when needed and validates its result
through the helper. Old environment class import paths and type/spec inputs
remain compatible. Image formats, hash rules, and build/publication are unchanged.

## Runtime implementations

`providers/opensandbox_yicloud/adapter.py` composes the extracted internal method
groups into the concrete Harbor `BaseEnvironment`. All method groups share that
one adapter instance and its existing state; they are not pluggable providers or
new lifecycle/transfer interfaces. The same provider package owns async command
transport, directory packing, S3 staging, and S3 profile resolution; these
internal helpers no longer have Harbor-root modules. See
[YiCloud responsibilities](providers/opensandbox_yicloud/README.md).

`providers/e2b_series/qz_legacy/adapter.py` preserves the qz implementation. Its old
`qz_e2b_sandbox.py` entrypoint re-exports the class and public qz configuration
helpers. Official E2B continues using Harbor's `E2BEnvironment` directly. See
[E2B series boundaries](providers/e2b_series/README.md).

`e2b_series` groups related implementations inside `providers/`, alongside the
YiCloud provider. It is not a separate top-level category of environments.

BackendPlan selection and provider extraction are followed by the launch policy
and image handoff described above. The serialized plan still contains only
type/spec; launch flags are derived process outputs. Existing Harbor capabilities
and provider lifecycle/transfer implementations remain the contracts; additional
core abstractions require actual consumers.

Local regression coverage lives in `tests/test_backend_plan.py`, including a
frozen Shell selection fixture and the legacy rollout override policy. Existing
qz, YiCloud, E2B runtime, configuration, command, and rollout suites protect the
callers. `test_environment_adapter_imports.py` uses installed Harbor/E2B APIs in
isolated processes to check that old and new class imports are identical,
concrete Harbor environments, and that importing qz does not patch official E2B.
`test_backend_launch.py` exercises failure propagation, image precedence,
manifest versions, and literal Shell argument handoff. Existing backend Shell
regressions protect the same launchers. These tests do not establish live
provider availability.
