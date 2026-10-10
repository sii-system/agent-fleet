# Harbor task image BuildKit instrumentation frontend

This component adds build-time instrumentation to dataset `RUN` instructions
without editing task Dockerfiles. APT, Git mirror, and direct download routing
share this mechanism.

The frontend is a build tool, not a task base image. BuildKit runs it to lower a
Dockerfile into LLB; the frontend itself contributes no layer to the resulting
task image.

## Design

`upstream.version` pins the official BuildKit source. `build.sh` checks out that
revision, applies the versioned patch, copies `instrumentation.go` into the
upstream Dockerfile frontend package, and compiles the static
`dockerfile-frontend` executable. `Dockerfile` packages that executable in a
`scratch` OCI image. The provider-neutral download shell wrapper and its awk
URL helper remain separate content-addressed secrets.

The patch adds one call immediately before upstream `dispatchRun` invokes
`llb.State.Run`. At that point upstream has already handled shell and JSON forms,
heredocs, custom shells, stage environment, and all `RUN` options. The hook does
not parse command text or rewrite the serialized LLB graph.

Every `RUN` receives these build-only inputs:

| Input | Effect |
| --- | --- |
| `PATH` | Prepends the enabled download wrapper directory and `/run/opensandbox-apt/bin` |
| `apt`, `apt-get` | Mount the same wrapper secret under both executable names |
| `source-rewriter.awk` | Mount the APT source rewriter |
| `gateway-root` | Mount the single Gateway root used to derive APT routes |
| `frontend-identity` | Include the frontend digest in the `RUN` cache key |
| `shadow` | Provide invocation-local writable state on tmpfs |
| optional `curl`, `wget` | Route explicit HTTP(S) arguments through one configured third-party source |
| optional `/etc/gitconfig` | Apply the configured GitHub mirror for the current RUN |
| optional `/etc/conda/condarc.d/99-agent-fleet-gateway.yaml` | Map defaults and conda-forge using normal conda/mamba rc discovery |

Wrapper, rewriter, and Gateway-root secret IDs include content digests because BuildKit
does not include secret contents in cache keys. The mounted files, PATH change,
and shadow state exist only while a `RUN` executes. The final image keeps the
Dockerfile's original environment and contains none of these files.

The manager supplies the local OCI layout as a named build context and selects
it with `BUILDKIT_SYNTAX`. This lets unchanged dataset Dockerfiles use the
instrumented frontend.

## Local build and cache

`ensure_frontend()` caches the OCI layout by upstream revision, patch, helper,
build scripts, and host architecture. It verifies cached OCI blobs before reuse
and serializes concurrent first builds with a per-key lock. Missing or corrupt
entries are rebuilt locally; no frontend image is pushed to or resolved from a
registry. The separately mounted runtime assets use content-addressed secret
IDs, so changing either shell or awk input also changes the ExecOp cache key.

The default cache is `/data/harbor-runs/opensandbox-frontend`. Override it with
`HARBOR_TASK_IMAGE_FRONTEND_CACHE`. A warm cache only needs Docker/buildx. A
cold build also needs Git, GNU timeout, and the pinned Go version installed by
the repository setup flow.

Cold builds honor provider-neutral source settings:

- `HARBOR_TASK_IMAGE_GOPROXY` and `HARBOR_TASK_IMAGE_GOSUMDB` for Go modules;
- optional `HARBOR_TASK_IMAGE_GITHUB_MIRROR_URL` for the BuildKit checkout,
  with official GitHub used when unset.

The prebuild launcher treats frontend preparation as shared infrastructure. A
failure returns exit code 78, stops new task dispatch, and is recorded once for
the batch instead of becoming many unrelated task failures. Fully cached task
batches do not prepare the frontend.

## Runtime boundary

`../dockerfile_renderer/strategies/apt/apt-wrapper.sh` and
`source-rewriter.awk` implement
APT routing. Normal PATH lookup of `apt` and `apt-get` reaches the mounted
wrapper, which creates shadow source files, invokes the real system binary, and
reconciles indexes without changing authored sources in the image.

`../dockerfile_renderer/strategies/download/download-wrapper.sh` and
`url-rewriter.awk`
implement the optional `curl`/`wget` route. The wrapper receives the source root
through a secret file, rewrites URLs only when the command runs, and execs the
image's real tool. This covers shell-expanded URLs and scripts produced in
earlier RUN instructions without parsing Dockerfile commands. Previously unseen
HTTP(S) targets do not require source registration. Invocations with custom
methods, bodies, credentials, headers, proxy settings, indirect config, or
unpreservable output behavior execute the original binary unchanged.

This adapter and its Gateway route are for the automated Harbor task image prebuild
path only. They are not supported as manually invoked curl/wget endpoints and
must not be present in the final image or ordinary Sandbox runtime.

The mechanism does not grant privileges or install dependencies. Absolute
`/usr/bin/apt-get`, PATH replacement, `env -i`, direct `execve`, libapt,
python-apt, aptitude, and nala can bypass APT interception. Absolute curl/wget
paths, PATH replacement, replacement binaries, and non-curl/wget HTTP clients
can likewise bypass download interception. The download wrapper is POSIX sh and
needs no bash; a stage invoking the APT wrapper must contain `/bin/bash`.
Custom Dockerfile frontends may also be incompatible because the manager
deliberately selects this pinned frontend.

Instrumentation identities invalidate BuildKit `RUN` cache when a build occurs.
They do not bypass the existing immutable task-image lookup; rebuilding an
already published task still uses the normal force-rebuild workflow.

## Validation and upgrades

`tests/test_frontend.py` covers orchestration and corpus preservation.
`tests/test_download_wrapper.py` covers fixed-route derivation, shell syntax,
direct fallback for non-cache-eligible URLs, and behavior-preserving cache
bypass.
`scripts/test-lowering.sh` runs the shared corpus through pristine and patched
BuildKit source trees, compares decoded ExecOps and image configuration, and
checks cache-key separation for each runtime input.

```bash
PYTHONPATH=Agents/utils/common/Harbor:. python -m unittest discover \
  -s Agents/utils/common/Harbor/task_image_manager/build_engine/frontend/tests \
  -p test_frontend.py -v

PYTHONPATH=. python -m unittest \
  Agents/utils/common/Harbor/task_image_manager/build_engine/frontend/tests/\
test_download_wrapper.py -v

FRONTEND_WORK_DIR=/data/<completed-frontend-work-dir> \
  bash Agents/utils/common/Harbor/task_image_manager/build_engine/frontend/scripts/test-lowering.sh
```

For an upstream upgrade, change the tag and commit together, reapply the small
`dispatchRun` hook, review upstream `State.Run` and mount semantics, compile with
the pinned toolchain, and run both checks above. If an upgrade requires hooks in
multiple semantic states or post-processing the LLB graph, reassess the design
instead of expanding this patch mechanically.
