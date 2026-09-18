# YiCloud OpenSandbox Quick Start

This guide runs a local Harbor benchmark framework task in a YiCloud
OpenSandbox instance. The
runner builds or reuses the task image, uploads the agent runtime, executes the
agent and verifier, collects the result, and deletes the instance.

## Prerequisites

- Install Docker with Buildx and log in to the target image registry.
- Prepare a local Harbor benchmark framework dataset whose tasks contain a
  Dockerfile or the
  supported Compose subset.
- Obtain YiCloud API credentials, a project name, and an OpenSandbox
  environment ID.
- Make the model gateway reachable from OpenSandbox.
- For the S3 upload backend, provide an `s3cmd` configuration and a writable
  bucket.

Compose tasks use a constrained service group: all services are created in the
same dedicated environment, receive a managed `/etc/hosts` alias block, and
must pass the capability gate. Shared volumes, fixed IPs, multiple networks,
and privileged/capability requests are rejected before any Sandbox is created.

## Configure

Copy the committed configuration template, then keep all credentials in the
git-ignored local file:

```bash
cp config.env config.local.env
```

Set at least the following values in `config.local.env`:

```bash
BASE_URL=https://model-gateway.example.com
API_KEY=your-model-api-key
MODEL=your-model-id

RL_ENVIRONMENT_TYPE=opensandbox
YICLOUD_PUBLIC_KEY=your-yicloud-public-key
YICLOUD_SECRET_KEY=your-yicloud-secret-key
YICLOUD_PROJECT_NAME=your-project
YICLOUD_SANDBOX_ENVIRONMENT_ID=env-xxxxxxxx-xxx

YICLOUD_HARBOR_HOST=harbor.example.internal
YICLOUD_HARBOR_PROJECT=seta
YICLOUD_HARBOR_USERNAME=your-harbor-username
YICLOUD_HARBOR_PASSWORD=your-harbor-password
# Set to 1 after the host trusts the OCI Registry certificate. Current YiCloud ingress
# is verified with 0.
YICLOUD_HARBOR_TLS_VERIFY=0

YICLOUD_SANDBOX_S3_PROFILE=provider-name
```

After saving the OpenSandbox backend configuration, run setup once from the
repository root. This prepares Go 1.25.4 for cold frontend builds from the
configured `HARBOR_OPENSANDBOX_GOPROXY` or its domestic default:

```bash
./scripts/setup.sh
```

Each provider is an ignored, project-local profile:

```text
.s3-profiles/provider-name/
├── profile.env
└── s3cfg          # optional; development-host write credentials
```

`profile.env` identifies one bucket, credential-free Sandbox read origin, and
immutable object prefix:

```bash
YICLOUD_SANDBOX_S3_BUCKET=your-bucket
YICLOUD_SANDBOX_S3_READ_ORIGIN=http://s3.internal.example/your-bucket
YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1
```

Create a separate directory for every S3 provider. Maintainers who publish new
objects may add a sibling `s3cfg` containing least-privilege write credentials;
read-only users should omit that file. The read origin must already address the
bucket and must not contain credentials, query parameters, or fragments. The
bucket policy must allow anonymous `GetObject` from Sandbox networks without
granting anonymous writes.

Agent Fleet computes the immutable object key locally and checks its ordinary
anonymous URL first. An existing object therefore requires no S3 key. Some S3
providers return `403` rather than `404` for a missing object when anonymous
`ListBucket` is intentionally disabled. In that case, a configured writer uses
an authenticated exact-key listing to distinguish a miss from an existing
object whose anonymous-read policy is broken. Writer credentials must therefore
include `ListBucket` restricted to the configured prefix as well as permission
to publish objects there. The `s3cfg` must be a regular file that is not group-
or world-readable. In `auto` mode, a missing object without a safe write
configuration uses the existing HTTP transport instead. In strict `s3` mode,
the same condition is an error.
Switch providers only by changing
`YICLOUD_SANDBOX_S3_PROFILE` in `config.local.env`. Agent Fleet rejects path
traversal, symlinked profile files, unknown or duplicate metadata keys, and
standalone S3 values that conflict with the selected profile. Selecting a
profile defaults `YICLOUD_SANDBOX_UPLOAD_BACKEND` to `auto`: S3 is preferred,
while an S3 staging or Sandbox materialization failure falls back to the
existing authenticated HTTP upload path. Set the value explicitly to `s3`
when failure must be strict. Without a profile, the bucket and read origin may
be configured directly; `YICLOUD_SANDBOX_S3_CONFIG` remains an optional legacy
write configuration.

Every YiCloud OpenSandbox create request uses the provider-required
`["sleep", "infinity"]` entrypoint. After the Sandbox reaches `Running` and
service aliases are installed, Agent Fleet launches the Bundle's resolved
start command through execd. This changes only OpenSandbox startup; task images
and other environment backends retain their original behavior.

Use the immutable environment ID in automation. The runner rejects requests
without an explicit environment ID or exact environment name.

## Run One Task

Start with one worker and one task:

```bash
cd Agents/utils/common/Harbor

AGENT=claude-code \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
HARBOR_INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
HARBOR_N_CONCURRENT=1 \
bash start.sh
```

The command prints the output and summary paths. For debugging, add
`YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL=1` and delete the retained instance after
inspection.

Image preparation first lists the task repository anonymously. If Harbor
returns 401 or 403, it retries once using the existing registry credentials
(`YICLOUD_HARBOR_USERNAME` / `YICLOUD_HARBOR_PASSWORD`, with the existing
local registry configuration fallback). Subsequent image inspection uses the
same credentials. Missing credentials or a failed authenticated query stop
preparation; they do not trigger a rebuild. Public projects remain usable
without credentials.

## Task Image Hash Validation

On-demand image preparation selects the task repository's most recently pushed
tag and validates it against the local content hash by default. The selected
tag must match `<service>-<first 20 hex characters of the local hash>`.
A mismatch or a tag without that hash encoding stops preparation; it does not
silently select an older image or rebuild. This checks the published hash
prefix, not the full remote hash or image contents.

To skip hashing for faster reuse, explicitly set
`HARBOR_OPENSANDBOX_VALIDATE_IMAGE_HASH=0` when starting workers, or pass
`--no-validate-image-hash` to the image manager. Only this opt-out path emits a
warning that the local dataset task definition may be inconsistent with the
remote image. `--validate-image-hash` re-enables validation and takes precedence
over the environment setting.

An empty repository still uses the existing hash-based build/push flow. This
option does not change prebuild uploaded-Bundle cache verification or
`--skip-hash-verification`.

## Optional: Prebuild Task Images

For a batch run, publish task images once before starting workers:

```bash
set -a
source config.local.env
set +a

# Required for Dockerfile RUN downloads from upstream release hosts.  Pin the
# development machine's local proxy instead of allowing a shell helper to
# fall back to a forwarded proxy; BuildKit defaults to host networking.
export HARBOR_OPENSANDBOX_BUILD_PROXY_URL=http://127.0.0.1:7890

HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY=4 \
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Datasets that intentionally keep internal builder locations out of their
Dockerfiles may use logical base names and resolve them only during image
preparation:

```bash
HARBOR_OPENSANDBOX_BASE_IMAGE_REGISTRY=harbor.example.internal/agent-fleet-task-base \
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  /absolute/path/to/Harbor-Dataset benchmark-project
```

The manager pins each resolved base manifest and passes it as a BuildKit named
context. With the variable unset, unqualified `FROM` entries keep ordinary
Docker Hub mirror resolution; with it set, an image that cannot be found under
the prefix fails the build instead of falling back to the mirror.

Every successful Registry resolution also updates a target-scoped local
uploaded-Bundle index under `HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT`. On restart,
prebuild recomputes each task's static environment hash and, when it still
matches the local record, skips Registry login and manifest inspection. For a
stable dataset, hashing can also be skipped:

```bash
HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION=1 \
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Fast resume trusts that the recorded Registry artifacts still exist and that
the task content has not changed. Leave the option at its default `0` to retain
local hash validation. Set
`HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE=0` to bypass the local
index entirely and restore per-task Registry lookup. Fast resume also defers
configured package-source health probes, so a batch made entirely of local
hits performs no per-task network request. If a real miss later fails while
building, the existing health check and trusted-source fallback still run.

APT source routing is build-runtime scoped and uses the same provider-neutral
Gateway root as direct downloads:

```bash
export HARBOR_OPENSANDBOX_DOWNLOAD_SOURCE_URL="http://<TRUSTED_SOURCE>/cache"
```

Every credential-free HTTP(S) repository URI is encoded from its scheme,
authority, and base path. APT can then append its normal `dists/...` and
`pool/...` paths, which the Gateway decodes back into the original upstream
URL. No local JSON map, repository registration, or named-source distribution
is required. Object-level URLs such as signing keys and setup scripts use the
same root through the `curl`/`wget` adapter. Without
`HARBOR_OPENSANDBOX_DOWNLOAD_SOURCE_URL`, original APT, `curl`, and `wget`
behavior is preserved.

The [instrumentation frontend](opensandbox_buildkit_frontend/README.md) mounts
an APT wrapper and prepends its directory to `PATH` for every Dockerfile `RUN`.
The wrapper rewrites the current source files in a temporary view, invokes the
rootfs `/usr/bin/apt*`, and reconciles downloaded indexes back to names derived
from the original sources. Authored `/etc/apt` files, the persistent stage
environment, and final image layers remain unchanged. Runtime asset identities
invalidate affected BuildKit cache entries without changing task-image identity.

Interception covers normal `apt` and `apt-get` lookup from shell, JSON exec,
nested or downloaded scripts, and subprocesses inheriting `PATH`. Absolute
paths, PATH replacement, `env -i`, libapt-based tools, and explicit custom APT
layouts can bypass it. Sources with credentials or queries that cannot be
represented safely remain direct and emit a credential-redacted bypass
warning. See the linked frontend document and
[image manager design](OPENSANDBOX_IMAGE_MANAGER.md#apt-build-runtime-interception)
for the runtime contract and upgrade boundary.

The manager builds and verifies the pinned frontend in a local OCI cache on
first use. A cold cache needs Git, GNU timeout, and the setup-managed Go; a
shared preparation failure stops further task dispatch for that batch.

Direct shell- or exec-form `curl` and `wget` downloads use that same
provider-neutral source root:

```bash
export HARBOR_OPENSANDBOX_DOWNLOAD_SOURCE_URL="http://<TRUSTED_SOURCE>/cache"
```

When enabled, the BuildKit frontend injects temporary `curl` and `wget` wrappers
ahead of PATH in every RUN. Each explicit HTTP(S) URL is routed at command
execution time using the source's versioned request convention:
`<root>/download/v1/<scheme>/<hex-authority>/root` for an origin root, or
`<root>/download/v1/<scheme>/<hex-authority>/object/<hex-path>` for an
object. Components use lowercase hex so reverse proxies never need to preserve
encoded slashes. The reversible selector keeps the real `/@root` object distinct from
the origin root and does not truncate long authorities. The source remains an
opaque cache service to Agent Fleet: the manager does not load its plan or
maintain a per-object or per-origin map. Runtime interception also covers
shell-expanded URLs and `curl`/`wget` inside dynamically produced scripts.
Previously unseen HTTP(S) URLs are filled on demand without a Gateway source
registration. Calls whose method, credentials, headers, proxy settings, indirect
configuration, or output semantics cannot be preserved bypass the cache and run
through the image's real tool unchanged. Calls using absolute executable paths
or a reset PATH are also outside this PATH-based adapter.

> **Prebuild-only:** the wrappers and dynamic download route are internal
> OpenSandbox prebuild machinery. Do not invoke the Gateway route manually, put
> it in task Dockerfiles, expose it publicly, or reuse it inside a running
> Sandbox. The mounts and source URL do not persist in the resulting image.
> Server-side prebuild authentication is planned separately from URL handling.

The Gateway is a cache accelerator, not a URL security policy. It must not
reject a new URL merely because analysis has not seen its origin or path.
Validate a small batch of tasks before the first full prebuild run.

Other package sources such as pip, npm, Go, Cargo, Rustup, Dart Pub, and Julia
retain their Docker build-argument behavior and are not part of APT runtime
interception.
`HARBOR_OPENSANDBOX_PUB_HOSTED_URL` and
`HARBOR_OPENSANDBOX_JULIA_PKG_SERVER` are optional provider-neutral URLs. When
configured, the manager passes them to Dockerfile `RUN` instructions as
`PUB_HOSTED_URL` and `JULIA_PKG_SERVER`; they may name a trusted third-party
service or a cache gateway and are not persisted in the published image.

Prebuild performs a bounded BuildKit cache prune before starting and every 30
minutes while it runs. Defaults are `max-used-space=500GB`,
`min-free-space=300GB`, and `reserved-space=100GB`; all four values are
configurable through `HARBOR_OPENSANDBOX_PREBUILD_GC_*`. Set
`HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC=0` to disable only periodic GC;
the initial prune still runs for every non-dry-run batch. It prunes only unused
BuildKit cache and does not delete images, containers, volumes, or artifacts
already published to the OCI Registry.

Already published content-addressed images are reused in the task-specific
repository, and every current run still gets its per-task Bundle Manifest even
on a local cache hit. Unsupported environment definitions are listed as
skipped in the prebuild report; unsupported runtime capabilities fail before
creation.

## Troubleshooting

- A scheduling timeout or long `Pending` state is a platform capacity issue;
  the log includes the Sandbox ID and latest status.
- An image preparation failure occurs before Sandbox creation. Verify Buildx,
  registry login, and the task Dockerfile.
- An artifact transport failure should be diagnosed separately from agent
  execution. Existing objects require only the anonymous read URL and bucket
  policy; publishing a missing object additionally requires a safe local
  `s3cfg`. Also verify DNS from the relevant host and Sandbox networks.
- A model request failure means the instance started, but its configured model
  gateway is unreachable or rejected the request.

See [task image management](OPENSANDBOX_IMAGE_MANAGER.md) for image naming,
caching, and registry internals. See [Harbor benchmark framework structure](STRUCT.md) for the full
configuration reference.

## Bounded artifact downloads

File downloads read 512 KiB chunks through the existing exec transport, write
straight to a temporary host file, and check the source size and SHA-256 before
replacing the target. Failed, corrupt or cancelled transfers preserve an
existing target and remove the partial file. This requires the image's existing
GNU-style `stat`, `sha256sum`, `dd` and `base64` utilities; no S3 write credentials
or new provider API is required.

Directory downloads create one temporary sandbox tar archive, use the same
bounded transfer, extract with Python's `data` safety filter, and remove the
remote archive in `finally`. Excluded-directory downloads use this path too.
Transport retries keep the existing idempotent exec behavior. A failed chunk
aborts the download after those retries; a later download starts again, rather
than claiming resumability across calls.

`HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY` defaults to `4`. Independent worker
processes running as the same host user share advisory-lock slots under
`${TMPDIR:-/tmp}/agent-fleet-artifacts-<uid>`; locks release on cancellation or
process death. `HARBOR_ARTIFACT_LOCK_DIR` can select a shared local directory.
All cooperating workers must use the same directory and concurrency setting.
Separate hosts/users or isolated temporary directories have separate limits.
Do not unlink slot files while workers are running.

The limit covers OpenSandbox download and extraction operations, not agent
execution or trajectory conversion. Encoded responses and decoded buffers are
bounded by chunk size. Archive metadata and trajectory conversion can still
consume memory proportional to entry/event counts; filesystem writes can still
occupy reclaimable page cache. Chunking adds requests and SHA-256 adds a remote
read pass, so measure collection throughput on the target provider.
