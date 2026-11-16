# E2B Backend

Agent Fleet uses Harbor's native E2B environment for E2B Cloud and compatible
deployments such as AgentENV. Fixed benchmarks and remote rollout share this
backend and the official Python SDK. Keep credentials in the git-ignored
`config.local.env` or host environment; `/run_trial` requests must not contain them.

## Dynamic Template Mode

For E2B Cloud, set the host configuration and use the SDK's endpoint defaults:

```bash
RL_ENVIRONMENT_TYPE=e2b
E2B_API_KEY=e2b_example
RL_E2B_SANDBOX_TIMEOUT_SEC=3600
```

Choose a sandbox lifetime within your account's limit and long enough for a
whole trial. Without this setting, Harbor requests 24 hours. The runner caps
that request using `RL_E2B_SANDBOX_TIMEOUT_SEC` (or the fixed-run override
`HARBOR_E2B_SANDBOX_TIMEOUT_SEC`).

For a self-hosted gateway, additionally set its SDK endpoints:

```bash
E2B_API_URL=https://e2b-api.example.com
E2B_SANDBOX_URL=https://e2b-gateway.example.com
```

The API URL is the SDK base; use the deployment's documented prefix. Do not
substitute the raw team REST base or `E2B_BASE_URL`. A shared `E2B_SANDBOX_URL`
requires a gateway that routes using the SDK's `E2b-Sandbox-Id` and
`E2b-Sandbox-Port` headers, as in AgentENV's recorded SDK runs. For wildcard
sandbox routing, leave that override unset and configure `E2B_DOMAIN` as the
deployment requires. For E2B Cloud, leave all three endpoint overrides unset.
Only disable client key-format validation (`E2B_VALIDATE_API_KEY=false`) when
the provider requires it. Do not enable `E2B_DEBUG` for benchmark runs.

Leave `E2B_TEMPLATE`, `RL_E2B_PREBUILT_TEMPLATE`,
`HARBOR_E2B_PREBUILT_TEMPLATE`, and custom `HARBOR_ENVIRONMENT_SPEC` overrides
unset to use the native path:

```text
Task docker_image / Dockerfile + context
                  |
Harbor task/environment-hash template alias
                  |
         Ready template exists?
           /              \
         yes              no
          |        E2B Template.build()
          |          CPU + memory
          +-------+-------+
                  |
        Fresh sandbox per trial
                  |
       Agent -> verifier -> artifacts
                  |
            Sandbox deletion
```

Harbor selects `Template.from_image()` when the task sets `docker_image`, or
`Template.from_dockerfile()` otherwise. Templates remain on the platform for
reuse. The shared `e2b_runtime.py` coordinates concurrent builds on the runner,
waits for the template's `default` tag, caps the sandbox lifetime, and uploads
offline verifier tools. There is no provider-specific template inventory or
task mapping. Registry credentials and image accessibility are the build
platform's responsibility.

### Native E2B Canary

Run explicit host setup first (`./scripts/setup.sh`). The canary uses the pinned
Harbor/E2B runner; startup does not install dependencies. It needs no model key,
Docker daemon, zellij session, or tracing service.

```bash
# Preview both native Harbor invocations; no cloud calls or API key required.
bash Agents/utils/common/Harbor/run_e2b_smoke.sh --dry-run

# Run two sequential oracle trials against the configured E2B service.
bash Agents/utils/common/Harbor/run_e2b_smoke.sh --execute
```

`OUTPUT_PATH` may specify a new output directory; existing directories are
refused. The canary selects native E2B, clears prebuilt/task-selection overrides,
disables tracing, and requests a 600-second sandbox lifetime unless configured
otherwise. Connection settings retain the shared config-loader precedence.

The task in `Tasks/Sandbox-smoke/e2b-native` exercises Dockerfile `COPY`/`RUN`,
solution/test upload, command execution, a verifier reward of 1, and artifact
download. Each trial must start without the previous trial's answer file. The
second trial targets the same reusable template; the first builds only if the
template is absent. This is not a cold-start benchmark.

After each successful Harbor invocation, the checker requires exactly one
finished native-E2B trial, no exception, reward 1, and the expected downloaded
artifact. It then queries all pages of running/paused sandboxes for that trial's
session metadata. Leftovers or an API failure fail the canary; it never deletes
unrelated sandboxes or templates. If Harbor fails before writing results, inspect
the run logs and platform inventory; absence of results is not proof of cleanup.

An oracle pass validates infrastructure, not model/agent installation. Follow
with a one-task Claude Code or OpenCode run before claiming real-agent support
on a new platform. Pi currently requires runtime delivery work for E2B. Network
isolation/allowlists, arbitrary Dockerfile compatibility, and platform capacity
need separate validation. AgentENV must be validated from a reachable host;
an E2B Cloud pass does not establish AgentENV compatibility.

Use the same `RL_ENVIRONMENT_TYPE=e2b` configuration with `run_fleet.sh` for
fixed benchmarks or the rollout command below for remote requests.

## Prebuilt Template Compatibility Mode

Use this only when an E2B-compatible deployment supports Sandbox lifecycle,
commands, and files but cannot yet run dynamically built task Templates:

```bash
RL_ENVIRONMENT_TYPE=e2b
RL_E2B_PREBUILT_TEMPLATE=your-platform-template-id
E2B_API_KEY=your-e2b-api-key
E2B_API_URL=https://e2b-api.example.com
E2B_DOMAIN=sandbox.example.com
```

`E2B_TEMPLATE` is accepted as a compatibility alias for
`RL_E2B_PREBUILT_TEMPLATE`. If the deployment exposes Sandbox envd over plain
HTTP, set `E2B_FORCE_HTTP=true`. Set `RL_E2B_SANDBOX_TIMEOUT_SEC` when the
platform requires an explicit lifetime.

The prebuilt mode intentionally skips the task Dockerfile and prepares only
the task workdir. A successful run proves compatibility with that prebuilt
Template, not general Dockerfile or dynamic Template support.

For verifier scripts that install uv from `astral.sh`, the runner reuses
`verifier-tools/curl`: it uploads host-prepared `uv`, `uvx`, and the narrow curl
shim after Sandbox creation. Other curl URLs still use the Sandbox system curl.

Start rollout with one worker first:

```bash
ROLLOUT=1 \
RL_HOST=127.0.0.1 \
RL_WORKERS=1 \
RL_MAX_CONCURRENT=1 \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
AGENT=oracle \
RL_AGENT=oracle \
bash Agents/utils/common/Harbor/start.sh
```

After a real smoke, check the structured result, verifier reward, pending and
active queues, running Sandboxes, and run-directory credential scan.
