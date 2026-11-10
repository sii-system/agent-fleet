# DSec Controller backend

This package implements Harbor's `BaseEnvironment` for DSec. One Harbor trial
owns one Sandbox, accessed exclusively through the public Controller
`sandbox-lifecycle/v1alpha1` API. Agent Fleet prepares immutable task images;
Controller selects and persists the owning Node; Node owns container lifecycle,
image leases and native filesystem operations. The adapter never accesses Node
hosts, Docker sockets, 3FS, EROFS mounts, or lower directories directly.

The compatibility entrypoint is `dsec_sandbox:DsecSandboxEnvironment`.
The shared backend selector resolves it when `HARBOR_ENVIRONMENT_TYPE=dsec`.
For launcher behavior, see [environment adapters](../../README.md).

## Prerequisites and configuration

The Controller must be reachable from the host runner. Task images must already
be published and resolvable by the DSec deployment as canonical OCI manifest
digest references, such as
`registry.example/tasks/example@sha256:<64-hex-digest>`; mutable tags and local
Docker builds are rejected. Image precedence is the `image_ref` environment
kwarg, the Bundle Manifest selected by `bundle_manifest_path` or
`HARBOR_OPENSANDBOX_BUNDLE_MANIFEST`, then `task.environment.docker_image`.

Every schedulable ready Node must advertise `capabilities.archive_transfer:
true`. For Pi, Claude Code and OpenCode, batch preflight additionally requires
the selected exact `PI_VERSION`, `CLAUDE_CODE_VERSION` or `OPENCODE_VERSION` in
`capabilities.agent_runtimes` on every ready Node. Agent runtimes are supplied
by the deployment's Toolkit; this backend does not upload per-trial agent
runtime caches. Preflight failure stops launch, without a guest-exec fallback.

Configure the shared launcher, for example:

```bash
HARBOR_ENVIRONMENT_TYPE=dsec \
DSEC_SANDBOX_CONTROLLER_ENDPOINT=https://controller.example \
AGENT=pi PI_VERSION=1.3.0 \
DATASET_NAME=auto DATASET_PATH=/absolute/path/to/tasks \
TOTAL_WORKERS=1 HARBOR_N_CONCURRENT=1 \
  bash Agents/utils/common/Harbor/start.sh
```

The endpoint must be an HTTP(S) origin without credentials, path, query or
fragment. Plain HTTP requires explicit `DSEC_SANDBOX_ALLOW_INSECURE_HTTP=1`.
HTTP clients do not inherit ambient proxy settings or follow redirects.
This client has no credential/header injection setting; configure the service
boundary accordingly before exposing it to untrusted networks.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DSEC_SANDBOX_CONTROLLER_ENDPOINT` | required | Public Controller origin |
| `DSEC_SANDBOX_ALLOW_INSECURE_HTTP` | `0` | Explicitly allow plaintext HTTP |
| `DSEC_SANDBOX_TTL_SECONDS` | `7200` | Sandbox lease lifetime |
| `DSEC_SANDBOX_READY_TIMEOUT_SEC` | `300` | Wait for Sandbox readiness |
| `DSEC_SANDBOX_CLOSE_TIMEOUT_SEC` | `120` | Wait for terminal close |
| `DSEC_SANDBOX_REQUEST_TIMEOUT_SEC` | `15` | Control HTTP timeout, at most 60 seconds |
| `DSEC_SANDBOX_EXEC_TIMEOUT_SEC` | `3600` | Default command wait timeout |
| `DSEC_SANDBOX_EXEC_OUTPUT_LIMIT_BYTES` | `16777216` | Requested execution output bound |
| `DSEC_SANDBOX_TRANSFER_MAX_BYTES` | `536870912` | Adapter transfer bound |
| `DSEC_SANDBOX_TRANSFER_TIMEOUT_SEC` | `1800` | Archive HTTP timeout; service deadlines also apply |

Constructor kwargs can override these numeric defaults. Services retain their
own protocol, output, capacity and deadline limits; increasing a client setting
does not increase service limits.

## Supported tasks and boundaries

This implementation supports Linux, a single container and root execution.
Docker Compose, Windows, GPUs and TPUs are unsupported. Read-only host bind
inputs are copied into the Sandbox; they are not live host mounts. Writable
mount declarations are accepted only for Harbor's managed `/logs/agent`,
`/logs/artifacts` and `/logs/verifier` outputs.

The adapter currently submits `shared/v1` with zero task resource quotas and
warns when a task requests CPU, memory or storage limits. The deployment must
support that policy; Nodes supporting only `unrestricted/v1` are incompatible
with the unmodified adapter. Resource capability flags do not imply that this
version enforces task quotas.

Initial networking selects `bridge` for public-network tasks and `none`
otherwise. Network allowlists are a temporary compatibility path: requested
allowlists produce a warning and remain on the public bridge. Dynamic egress
restriction is not enforced. Tasks requiring that isolation must not rely on
these advertised compatibility flags.

## Lifecycle and ownership

`start()` validates the definition, submits create with a stable request ID,
waits for `ready` and a reachable owning Node, then prepares Harbor directories,
optional task environment files and read-only inputs. Image delivery is
`lazy-required` with `fallback: none`; `force_build` is not forwarded.
Retryable create failures reuse the same identity. A failed startup with a
known Sandbox ID attempts a shielded close before propagating the error.

`stop()` requests close and waits for `closed` or `create_failed`. Retaining a
stopped Sandbox is unsupported: `delete=False` emits a warning and still closes
it. Image cache reclamation belongs to the deployment, not this adapter.

## Command execution

Harbor commands run as `/bin/bash -lc` under root. Commands larger than 4096
UTF-8 bytes are uploaded as private script files and run with `/bin/bash`.
Scripts remain owned by their Sandbox and are reclaimed at close. The requested
working directory and merged task/command environment are passed explicitly.

Execution uses non-TTY asynchronous resources, polls to a terminal state and
retrieves separate bounded stdout/stderr ranges. It does not provide realtime
streaming; the output callback receives completed output. Truncated streams and
`recovery_required` are reported as errors. Because the API has no execution
cancel operation, a command timeout or caller cancellation closes the entire
Sandbox. Actual task and agent commands still require their declared guest
runtime, including Bash for this execution interface.

## Native filesystem transfers

File transfer and environment preparation do not depend on Python, tar, chmod,
mkdir, pwd or cleanup commands inside the task image. Only task/agent commands
execute there.

- `PUT /v1alpha1/sandboxes/{id}/archive?path=/` sends an uncompressed tar with a
  known Content-Length and `Content-Type: application/x-tar`.
- `GET /v1alpha1/sandboxes/{id}/archive?path=/absolute/container/path` streams an
  uncompressed tar. The same endpoints exist on Node's internal API.
- Paths are normalized absolute container paths. The single value `.` refers
  to the OCI working directory, resolved by Node from runtime metadata.
- Services limit each transfer to 512 MiB of tar bytes, additionally bounded
  by the adapter's limit and service request deadlines.

Controller routes to the persisted owning Node. Node validates ready state and
actual container ownership, then calls Moby's native archive API. Moby owns
filesystem isolation and locking. Transfers create no execution records or
container temporary archives.

Uploads have Docker archive-copy semantics: an error can leave partial writes.
They are not transactional and are never transparently retried. Downloads are
bounded, validated and staged on the host before committing local output.
Unsafe archive paths, escaping links and special files are rejected. Input
read-only modes and log-directory permissions are written into tar headers on
the host. Host staging uses `TMPDIR`; allow enough space for uncompressed trees.

Archive requests use `DSEC_SANDBOX_TRANSFER_TIMEOUT_SEC`, independently of the
control request timeout; service deadlines still apply. Cancellation waits for
owned HTTP, packing and extraction workers before releasing temporary paths or
returning to the caller. It does not interrupt synchronous filesystem work or
roll back completed writes. Downloaded links are resolved against the exported
subtree before merging, including link chains. The directory merge itself is
not transactional.

For `download_dir("/", ...)` only, Moby's `/` and `/path` archive names and
root-relative hardlink targets are rebased into local staging. Other downloads
still reject absolute member names. Traversal and absolute symlink targets
remain rejected, including in root exports.

## Source layout and validation

| Module | Responsibility |
| --- | --- |
| `adapter.py` | Harbor interface, configuration, task/resource/network boundaries |
| `runtime_preflight.py` | Batch-wide Node archive and exact Toolkit capability checks |
| `lifecycle.py` | Stable create identity, readiness, startup compensation and close |
| `execution.py` | Command resources, output ranges, deadlines and cancellation |
| `transfer.py` | Host archive packing/extraction and Harbor input/output preparation |
| `archive_client.py` | Bounded binary archive HTTP transport |
| `client.py` | Public control API, stable errors and bounded response decoding |

Run the focused regression suites from the repository root with the prepared
Harbor runner and pinned Ruff version:

```bash
PYTHONPATH=. python -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_dsec*.py' -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/harbor_environment_adapters/providers/dsec_controller \
  Agents/utils/common/Harbor/tests/test_dsec*.py
```

Unit tests cover lifecycle, execution, host file semantics, transport failures
and capability preflight. They do not prove deployment compatibility. A live
check must also establish accepted resource policy, immutable image resolution,
Toolkit capability, real transfer, command execution and exact Sandbox cleanup.
