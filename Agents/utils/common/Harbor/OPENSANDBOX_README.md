# YiCloud OpenSandbox Quick Start

This guide runs a local Harbor benchmark framework task in a YiCloud
OpenSandbox instance. The
runner builds or reuses the task image, uploads the agent runtime, executes the
agent and verifier, collects the result, and deletes the instance.

## Prerequisites

- Run `./scripts/setup.sh` once from the repository root.
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

APT source routing is build-runtime scoped. `--apt-mirror` provides the Ubuntu
and Debian mirror root. Third-party repositories, signing keys, and bootstrap
downloads use an explicit provider-neutral prefix map; Agent Fleet does not
guess mirror paths:

```bash
export HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON='{
  "https://packages.example.com/repository": "http://<TRUSTED_MIRROR>/apt/vendor-repository",
  "https://packages.example.com/signing-key.gpg": "http://<TRUSTED_MIRROR>/objects/vendor-key.gpg"
}'
```

The generated Dockerfile gives every shell-form `RUN` the same ephemeral wrapper
directory at the front of `PATH`. Ordinary `apt` and `apt-get` calls therefore
read the source files that exist at invocation time, create an invocation-local
shadow tree, and apply the longest exact-or-`/`-boundary source mapping there.
The wrapper passes the shadow paths to the current `/usr/bin/apt*` binary through
APT's `Dir::Etc::SourceList` and `Dir::Etc::SourceParts` options. Unmatched
active HTTP(S) sources remain usable and produce a warning containing both URI
and source-file path; `file:`, `cdrom:`, and other non-HTTP(S) sources remain
quiet. Real `/etc/apt` content is never changed. Runtime helper contents
participate in content-addressed BuildKit secret IDs, so helper changes invalidate
cached RUNs when a build occurs. They do not participate in the task's static
image identity.

Following a successful `apt update` or `apt-get update`, the wrapper uses
`apt-get indextargets` against the rewritten and original source views to copy
the downloaded package and release metadata to the filenames associated with
the original URIs. Final images can therefore consume their existing package
lists with the unchanged `/etc/apt` sources; reconciliation failure is reported
as `event=index-reconciliation-failed` and fails that invocation. This remains
invocation-local and does not add static stage detection or a cleanup RUN.

This includes nested shells, dynamically downloaded scripts, and subprocesses
that use normal `PATH` lookup. The implementation intentionally permits JSON
exec-form APT invocations, `/usr/bin/apt`, `/usr/bin/apt-get`, other explicit
binary paths, and custom libapt frontends to bypass interception. `APT_CONFIG`
and explicit custom source/config layouts also warn and bypass instead of being
parsed. The wrapper assets, source map, shadow tree, and `PATH` change are
BuildKit-only mounts or shell-local state and do not enter the final image.
There is no static APT detection, source mutation tracking, snapshot, restore,
`/usr/bin` overlay, or apt-binary mount.

Dockerfile `RUN --<option>=<value>` prefixes, including `--device`, are preserved
as RUN options when shell runtime mounts are added. Git `/etc/gitconfig` mounts
remain independent and continue to work on both shell-form and JSON exec-form
RUN instructions.

The same explicit map remains available for direct shell- or exec-form `curl`
and `wget` build-transport URLs, including signing keys and bootstrap objects.
Other `RUN` data, APT source-file writes, persisted data heredocs, and
`COPY <<EOF` data remain task-authored; command heredocs only rewrite configured
URLs used by direct `curl` or `wget` commands. Unconfigured third-party URLs are
never guessed or silently rewritten.

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
