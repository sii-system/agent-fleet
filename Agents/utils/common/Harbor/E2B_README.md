# E2B Rollout Quick Start

Agent Fleet supports Harbor's built-in dynamic E2B environment and an opt-in
prebuilt-template compatibility mode. Keep E2B credentials and template IDs in
the git-ignored `config.local.env`; `/run_trial` requests must not contain them.

## Dynamic Template Mode

Set the host configuration and leave the prebuilt template unset:

```bash
RL_ENVIRONMENT_TYPE=e2b
E2B_API_KEY=your-e2b-api-key
```

Harbor converts each task Dockerfile into an E2B Template and then creates a
Sandbox from that Template.

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
