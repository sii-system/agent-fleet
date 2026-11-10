# YiCloud OpenSandbox implementation

Harbor loads the concrete environment through the preserved spec
`yicloud_opensandbox:YiCloudOpenSandboxEnvironment`. That module is now a
compatibility import of `adapter.YiCloudOpenSandboxEnvironment`.

This environment spec is the only YiCloud/OpenSandbox compatibility module
kept at the Harbor root. Async command transport, directory packing, S3
staging, and S3 profile resolution are internal provider modules; repository
callers use their package paths rather than root-level facades.

The former 3175-line provider is split by its existing responsibilities:

| Module | Responsibility |
| --- | --- |
| `adapter.py` | Harbor interface, instance initialization, capabilities, preflight, task-image validation |
| `config.py` | Existing required-setting, integer/boolean, and managed-registry validation |
| `client.py` | Lazy YiCloud SDK, authentication retry, environment lookup, image/binding normalization, execd endpoints and signing |
| `lifecycle.py` | Single sandbox create/readiness/start/stop, cleanup and deletion confirmation, retain policy |
| `services.py` | Existing Bundle compatibility, service graph, depends_on/healthchecks, aliases, sidecar exec/stop and group cleanup |
| `execution.py` | Execd commands, SSE, verifier runtime activation, dispatch retry/idempotence, detached command polling and native command pool |
| `transfer.py` | Host artifact packing/extraction, S3 staging selection, read-only mount delivery and native file transport orchestration |
| `async_commands.py` | Bounded native-background execd transport, polling, output retrieval, cancellation and cleanup |
| `native_files.py` | Raw binary multipart upload, byte-compatible gateway signing and bounded native Range downloads; no guest exec |
| `packing.py` | Bounded directory archive workers used by native async uploads |
| `s3_transfer.py` | Content-addressed S3 staging, anonymous-read verification, local cache and the preflight/prewarm CLI |
| `scripts/s3_profile.sh` | Project-local S3 profile validation and environment projection sourced during Harbor bootstrap |

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

## Native file transport

File bytes travel through execd's `/files/upload`, `/files/info` and
`/files/download` APIs, initiated by the host runner. Uploads use one raw binary
multipart request with file mode in metadata. They never use guest Python,
Base64 decoding, `cat` assembly or stdout. Downloads validate 512 KiB HTTP ranges
and source metadata before replacing a host temporary file. File operations
create no command executions; directory packing/extraction and cleanup remain
separate guest commands using `tar`, `mkdir` and `rm`.

`YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN` selects the existing token-authenticated
upload origin. Empty selects the signed execd proxy. SDK 0.4.1 decodes signed
bodies as UTF-8, so `native_files.py` computes the same timestamp/query/HMAC-SHA256
protocol over raw multipart bytes instead. Signing and transmission stream a
host-owned snapshot using bounded buffers; neither payload nor credentials are
passed as subprocess arguments. Host staging respects `TMPDIR` and requires
space for the complete multipart body.

Uploads are nontransactional copies. Only connection-establishment failures
(DNS, refused connection or connect timeout) retry, at most twice, before any
request bytes reach the server. HTTP responses, including 503 with Retry-After,
read timeouts and ambiguous disconnects are never replayed. A failure can leave
partial remote output; there is no exec fallback. Download
failures preserve an existing host target, remove owned temporary files and
close HTTP responses. Cancellation waits for owned synchronous file workers
before releasing archives or download slots. Requests have socket timeouts;
this does not provide immediate worker interruption or a whole-file wall-clock
deadline against a continuously responding server.

Downloads check exact `Content-Range`, decoded byte counts and file metadata
before/after transfer. The file API does not supply a cryptographic source
checksum: metadata comparison does not detect a same-size rewrite that also
preserves all reported metadata. The former guest `sha256sum` pass is removed.
Existing host target modes are retained; new download files start private.

### S3 and legacy configuration

`http` delivers directly through the native file API. `s3` and `auto` retain the
existing deterministic local snapshot, object publication and anonymous-read
checks, but Sandbox delivery now uploads that local snapshot through the file
API. The Sandbox never runs curl, wget, Python or a Bash downloader. This changes
the data path: all payload bytes pass through the host-to-Sandbox upload channel,
including when the S3 object already exists. Direct Sandbox-to-S3 offload and
its bandwidth behavior are no longer provided by this adapter.

`auto` may fall back to ordinary HTTP when S3 staging fails, before Sandbox
mutation begins. A native upload or extraction failure is propagated without
replaying through another backend. Strict `s3` still fails on staging errors.
`YICLOUD_SANDBOX_S3_DOWNLOAD_TIMEOUT_SEC` and the old Oracle inline archive size
setting are no longer used. The legacy Oracle `inline` solution-upload spelling
is accepted with a warning and uses native file delivery plus ordinary archive
extraction; task files are never embedded in command arguments.

### Validation

Focused coverage is in `tests/test_yicloud_native_files.py` and
`tests/test_yicloud_opensandbox.py`. The former uses a real local HTTP server to
check raw binary HMAC, multi-range/empty transfers, modes, invalid ranges,
metadata changes, bounded connection retries, non-replayed delivery failures
and cancellation ownership. The
latter retains lifecycle, command and real tar/extraction regressions.

The 2026-11-10 live check used one existing immutable Tmax task on YiCloud.
Direct and gateway-signed uploads each round-tripped binary data larger than
1 MiB with matching host SHA-256, while a guard rejected any file-transport
call to guest exec. Task tests were uploaded and downloaded, the original
verifier was executed, logs were collected and Sandbox deletion was confirmed.
This verifies file transport and the verifier path; it is not an agent-solving,
throughput or long-running stability result. S3 publisher behavior remains
covered separately from the live file-channel check.
