# YiCloud OpenSandbox implementation

Harbor loads the concrete environment through the preserved spec
`yicloud_opensandbox:YiCloudOpenSandboxEnvironment`. That module is now a
compatibility import of `adapter.YiCloudOpenSandboxEnvironment`.

The former 3175-line provider is split by its existing responsibilities:

| Module | Responsibility |
| --- | --- |
| `adapter.py` | Harbor interface, instance initialization, capabilities, preflight, task-image validation |
| `config.py` | Existing required-setting, integer/boolean, and managed-registry validation |
| `client.py` | Lazy YiCloud SDK, authentication retry, environment lookup, image/binding normalization, execd endpoints and signing |
| `lifecycle.py` | Single sandbox create/readiness/start/stop, cleanup and deletion confirmation, retain policy |
| `services.py` | Existing Bundle compatibility, service graph, depends_on/healthchecks, aliases, sidecar exec/stop and group cleanup |
| `execution.py` | Execd commands, SSE, verifier runtime activation, dispatch retry/idempotence, detached command polling and native command pool |
| `transfer.py` | HTTP/fast upload, S3/auto fallback, read-only mount delivery, bounded downloads and safe archive extraction |

Internal method groups share the adapter's existing state and call each other
through `self`. They have no independent construction, registration, lifecycle
state machine, or provider configuration. The SDK remains lazy and version
pinned; no public OpenSandbox SDK is introduced.

The runtime preserves `sleep infinity` allocation,
multi-service behavior, bounded transport, retry boundaries, retain policy, and
start-failure cleanup. It does not add a generic Compose or Sandbox framework.

Concurrent deletion confirmations in one event loop share a complete paginated
listing for the same SDK service, project, and environment. A caller arriving
after a scan starts requires a new scan; completed snapshots are discarded when
no callers remain. Single and group deletion APIs remain unchanged, and listing
failures never count as confirmed absence. Sharing is process-local, so separate
runner processes still perform independent scans. The cleanup wait budget now
covers listing and lock waits as well as polling sleeps. A synchronous SDK call
already running in a thread may finish after its caller times out or cancels.

The local YiCloud tests exercise the legacy class entrypoint and
patch each dependency in its new owning module. Real platform, agent, verifier,
observation, and cleanup validation belong to live smoke and must be reported
separately from these local tests.
