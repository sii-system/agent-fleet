# Runfield Controller backend

This package implements Harbor's `BaseEnvironment` for Runfield. One Harbor trial
owns one Sandbox, accessed exclusively through the public `sii-runfield`
(`runfield`) async Python SDK against the Controller `sandbox-lifecycle/v1alpha1`
API (either over a local `sandbox-client-proxy` Unix domain socket or direct
HTTP/HTTPS). Agent Fleet prepares immutable task images; Controller selects and
persists the owning Node; Node owns container lifecycle, image leases and native
filesystem operations. The adapter never accesses Node hosts, Docker sockets,
3FS, EROFS mounts, or lower directories directly.

The stable entrypoint is `runfield_sandbox:RunfieldSandboxEnvironment`.
The shared backend selector resolves it when `HARBOR_ENVIRONMENT_TYPE=runfield`.
Importing the adapter or running `preflight()` does not import `runfield` or
open network sockets; `sii-runfield` is loaded lazily when a Controller
operation runs, and all `RunfieldSandboxEnvironment` instances on the same
asyncio event loop share one `runfield.SandboxClient` per transport key.
For launcher behavior, see [environment adapters](../../README.md).

## Prerequisites and configuration

The Controller must be reachable from the host runner (or via a client-host
`sandbox-client-proxy` daemon). Task images must already be published and
resolvable by the Runfield deployment as canonical OCI manifest digest
references, such as `registry.example/tasks/example@sha256:<64-hex-digest>`;
mutable tags and local Docker builds are rejected. Image precedence is the
`image_ref` environment kwarg, the Bundle Manifest selected by
`bundle_manifest_path` or `HARBOR_OPENSANDBOX_BUNDLE_MANIFEST`, then
`task.environment.docker_image`.

Every schedulable ready Node must advertise `capabilities.archive_transfer:
true`. For Pi, Claude Code and OpenCode, batch preflight additionally requires
the selected exact `PI_VERSION`, `CLAUDE_CODE_VERSION` or `OPENCODE_VERSION` in
`capabilities.agent_runtimes` on every ready Node. Agent runtimes are supplied
by the deployment's Toolkit; this backend does not upload per-trial agent
runtime caches. Preflight failure stops launch, without a guest-exec fallback.

Configure the shared launcher, for example:

```bash
HARBOR_ENVIRONMENT_TYPE=runfield \
RUNFIELD_CONTROLLER_ENDPOINT=https://controller.example \
RUNFIELD_CLIENT_PROXY_SOCKET=/run/runfield/client-proxy.sock \
AGENT=pi PI_VERSION=1.3.0 \
DATASET_NAME=auto DATASET_PATH=/absolute/path/to/tasks \
TOTAL_WORKERS=1 HARBOR_N_CONCURRENT=1 \
  bash Agents/utils/common/Harbor/start.sh
```

The endpoint must be an HTTP(S) origin without credentials, path, query or
fragment. Plain HTTP requires explicit `RUNFIELD_CONTROLLER_ALLOW_INSECURE_HTTP=1`.
HTTP clients do not inherit ambient proxy settings or follow redirects.
This client has no credential/header injection setting; configure the service
boundary accordingly before exposing it to untrusted networks.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `RUNFIELD_CONTROLLER_ENDPOINT` | required | Public Controller origin |
| `RUNFIELD_CONTROLLER_ALLOW_INSECURE_HTTP` | `0` | Explicitly allow plaintext HTTP |
| `RUNFIELD_CLIENT_PROXY_SOCKET` | unset | Optional client-host `sandbox-client-proxy` UDS path |
| `RUNFIELD_CLIENT_PROXY_MODE` | `auto` (`uds` when socket set) | SDK transport mode (`auto`, `uds`, or `direct`) |
| `RUNFIELD_SANDBOX_TTL_SECONDS` | `7200` | Sandbox lease lifetime |
| `RUNFIELD_SANDBOX_READY_TIMEOUT_SEC` | `300` | Wait for Sandbox readiness |
| `RUNFIELD_SANDBOX_CLOSE_TIMEOUT_SEC` | `120` | Wait for terminal close |
| `RUNFIELD_SANDBOX_REQUEST_TIMEOUT_SEC` | `15` | Control HTTP timeout, at most 60 seconds |
| `RUNFIELD_SANDBOX_EXEC_TIMEOUT_SEC` | `3600` | Default command wait timeout |
| `RUNFIELD_SANDBOX_EXEC_OUTPUT_LIMIT_BYTES` | `16777216` | Requested execution output bound |
| `RUNFIELD_SANDBOX_TRANSFER_MAX_BYTES` | `536870912` | Adapter transfer bound |
| `RUNFIELD_SANDBOX_TRANSFER_TIMEOUT_SEC` | `1800` | Archive HTTP timeout; service deadlines also apply |

Constructor kwargs can override these settings. Services retain their own
protocol, output, capacity and deadline limits; increasing a client setting does
not increase service limits.

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

`start()` validates the definition, submits `create_sandbox` (`wait_until=accepted`)
with a deterministic request ID, records the returned Sandbox ID immediately,
waits for `ready` with a reachable owning Node via `wait_sandbox_ready` (zero
Python polling sleep when using `sandbox-client-proxy`), and then uploads
optional task environment files and read-only inputs. Admission control, batch
state polling and retryable backoff are owned by `sandbox-client-proxy` and
`sii-runfield` rather than adapter-private semaphores or loops. Harbor output
directory creation (`/logs/agent`, `/logs/verifier`, `/logs/artifacts`,
`/artifacts`) is deferred out of `start()` so high-concurrency startup bursts
issue zero empty-directory `PUT /archive` RPCs: the first `exec()` under an
ordinary working directory creates those directories in an isolated outer shell
step (marking them prepared once an execution exits `0`), while a first `exec()`
whose working directory targets `/logs` or `/artifacts` — as well as zero-exec
trials or post-failure downloads — lazily materializes them via
`_prepare_harbor_directories()` before use.
Image delivery is `lazy-required` with `fallback: none`; `force_build` is not
forwarded. A failed startup with a known Sandbox ID attempts a shielded close
before propagating the error.

`stop()` calls `close_sandbox(..., wait=True)` and waits for `closed` or
`create_failed`, evicting the active SDK handle once terminal close succeeds.
Retaining a stopped Sandbox is unsupported: `delete=False` emits a warning and
still closes it. Image cache reclamation belongs to the deployment, not this
adapter.

## Command execution

Harbor commands run under root without a login shell:

- Commands up to `4096` UTF-8 bytes without NUL bytes run directly as
  `["/bin/bash", "-c", command]`.
- Commands in `4097..98304` UTF-8 bytes (`4 KiB..96 KiB`, safely below Linux
  `MAX_ARG_STRLEN`) without NUL bytes split on valid UTF-8 codepoint boundaries
  into `<= 4096`-byte positional chunks and run inline via an unexported random
  `__rf_cmd_<hex>` positional-shift bootstrap (`while [ "$#" -gt 0 ]; do ...; shift; done; export -n ...; exec /bin/bash -c "$..."`)
  with zero host temporary files, zero caller environment mutation and zero
  `PUT /archive` RPCs.
- Commands exceeding `98304` UTF-8 bytes (or containing NUL bytes) fall back to
  uploading a private script under `/tmp` via `upload_file` and executing
  `["/bin/bash", remote_path]`.

Execution uses two-phase submission (`create_execution` followed by
`wait_execution`, skipping the wait RPC when an accepted record is already
terminal with zero committed bytes and no truncation). It does not provide
realtime streaming; the output callback receives completed output. Truncated
streams and `recovery_required` are reported as errors.

On wait timeout or caller cancellation, `_abort_execution_on_wait_exit` first
attempts single-execution `cancel_execution`. When the execution transitions to
`canceled` (or `state_conflict` confirms natural terminal completion via
`execution_status`), the Sandbox remains alive. Closing the Sandbox is used only
as a fallback when `close_on_timeout=True` and single-execution cancellation
cannot guarantee termination. Best-effort probes pass `close_on_timeout=False`
so a probe timeout never destroys the shared Sandbox.

## Native filesystem transfers and path probes

File transfer, directory preparation and clean path probes (`is_dir` / `is_file`
via `GET /v1alpha1/sandboxes/{id}/archive/stat`) do not depend on Python, tar,
chmod, mkdir, pwd or cleanup commands inside the task image.

- `PUT /v1alpha1/sandboxes/{id}/archive?path=/` streams an uncompressed tar with
  a known `Content-Length` (`1..512 MiB`) and `Content-Type: application/x-tar`.
- `GET /v1alpha1/sandboxes/{id}/archive?path=/absolute/container/path` streams an
  uncompressed tar to host staging.
- Paths are normalized absolute container paths. The single value `.` refers
  to the OCI working directory, resolved by Node from runtime metadata.

Uploads have Docker archive-copy semantics: an error can leave partial writes,
and uploads are never transparently retried. Downloads are bounded, validated
and staged on the host before committing local output. Unsafe archive paths,
escaping links and special files are rejected.

For `download_dir("/", ...)` only, Moby's `/` and `/path` archive names and
root-relative hardlink targets are rebased into local staging. Other downloads
still reject absolute member names. Traversal and absolute symlink targets
remain rejected, including in root exports.

## Source layout and validation

| Module | Responsibility |
| --- | --- |
| `adapter.py` | Harbor interface, configuration, task/resource/network boundaries |
| `runtime_preflight.py` | Batch-wide Node archive and exact Toolkit capability checks |
| `lifecycle.py` | Stable create identity, two-phase readiness, startup compensation and close |
| `execution.py` | Two-phase execution wait, UTF-8 chunked argv, single-command cancel and stat probes |
| `transfer.py` | Host archive packing/extraction and lazy Harbor output directory preparation |
| `archive_client.py` | Bounded archive upload/download and container-external stat delegation |
| `client.py` | Lazy `sii-runfield` loader, loop-scoped shared `SandboxClient`, handle map and error translation |

Run the focused regression suites from the repository root with the prepared
Harbor runner and pinned Ruff version:

```bash
PYTHONPATH=. python -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_runfield*.py' -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/harbor_environment_adapters/providers/runfield_controller \
  Agents/utils/common/Harbor/tests/test_runfield*.py
```

Unit tests cover lazy SDK loading, shared per-loop `SandboxClient` pooling,
lifecycle, UTF-8 chunked command delivery, single-command cancellation, host
file semantics, transport failures and capability preflight in standalone CI
mode without requiring `sii-runfield` at import time.
