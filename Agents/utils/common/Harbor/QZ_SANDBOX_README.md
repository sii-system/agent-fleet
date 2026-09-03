# qz Sandbox Quick Start

This guide runs Harbor tasks in qz (SII Inspire, qz.sii.edu.cn) sandboxes:
each task executes in an isolated, disposable sandbox instance managed by the
platform.

## Prerequisites

- A qz platform account that belongs to a project.
- A machine on the SII internal network (it must reach
  `qz-sbx-api.sii.edu.cn`), for example a platform CPU Notebook.
- `./scripts/setup.sh` has been run from the repository root, and the model
  gateway is configured (`BASE_URL` / `API_KEY` / `MODEL` in
  `config.local.env`).

## Platform-side setup (web console)

Entry point: 作业中心 (Job Center) → Sandbox.

1. **Create a Sandbox Key** on the「Sandbox Key」tab and copy the key
   (starts with `sbx_`).
2. **Create a Template** (the sandbox boot image) on the「Template 列表」tab:
   - Name: letters, digits, and underscores only;
   - Compute spec: fixed on the Template (e.g. g.c2 = 2 vCPU / 8 GB); create
     another Template for a different spec;
   - Sandbox Key: must be the key from step 1 — Templates are bound to a key;
   - Image: an official image (e.g. `sandbox-base`, Ubuntu 24.04 +
     Python 3.12), or a custom image pushed to the platform image registry
     (镜像管理) first.
3. Wait until the Template status is ready.

For the repository-side `create`, `list`, and `get` workflow, see the
[QZ Template Manager](QZ_TEMPLATE_MANAGER.md).

## Repository-side configuration

Add to `config.local.env` (never commit the key):

```bash
RL_ENVIRONMENT_TYPE=qz
SBX_API_KEY=sbx_xxx                # from step 1
# Select exactly one:
QZ_SANDBOX_TEMPLATE_MAP=/absolute/path/to/qz-templates.json
# QZ_SANDBOX_TEMPLATE=your_template  # fixed Template name or ID
# QZ_SANDBOX_TIMEOUT_SEC=14400     # max sandbox lifetime; 4h is the platform cap
# NPM_CONFIG_REGISTRY=https://registry.npmjs.org
# QZ_NODE_DIST_URL=https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.gz
```

Then run the normal Agent Fleet setup. It reads the saved qz backend before
checking prerequisites, so a qz runner host does not need Docker:

```bash
bash scripts/setup.sh
```

If a Harbor runner environment was installed before this provider existed,
rebuild it once:

```bash
rm -rf ~/.local/share/agent-fleet/harbor-runner
bash Agents/utils/common/Harbor/setup_runner_env.sh
```

## Run one task

```bash
cd Agents/utils/common/Harbor

AGENT=oracle \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
HARBOR_N_CONCURRENT=1 \
bash start.sh
```

Scale up the worker count after a single task passes. The launcher accepts
`AGENT=oracle` (reference solutions), `AGENT=claude-code`, and
`AGENT=opencode` (real agents) on qz.

QZ create traffic is shaped automatically. Benchmark users choose only the
normal worker count: the adapter keeps at most 10 `Sandbox.create` calls in
flight across all local worker processes, releases each slot as soon as one
create returns, and continues until the requested workers are active. This
does not cap active task concurrency. `QZ_CREATE_CONCURRENCY` is an
operator-only override for deployments where QZ has confirmed a different
safe create window, for example after adding nodes to the type group. It is
not a fleet CLI or task setting, and normal benchmark runs need no additional
configuration.

The same OpenCode run is available through the repository-level fleet entry
point; the backend, key, and current Template come from `config.local.env`:

```bash
./scripts/run_fleet.sh \
  --taskset /absolute/path/to/Harbor-Dataset \
  --task 0 \
  --agent opencode \
  --workers 1
```

## Real agents

qz does not expose Notebook-host bind mounts to a Sandbox. Real-agent setup
therefore installs its runtime inside the Sandbox instead of depending on a
mounted runner cache or inbound access to the runner's temporary HTTP server.
Public endpoints can be reachable, but that availability is not treated as a
platform contract. The qz defaults use npmmirror; `NPM_CONFIG_REGISTRY` and
`QZ_NODE_DIST_URL` can select npmjs/nodejs.org or private sources explicitly.

"Host bind mount" here means mounting a path from the runner Notebook into the
Sandbox. It does not describe or rule out E2B-managed storage capabilities.

### claude-code (via the launcher)

`AGENT=claude-code` works with the normal launcher flow (`bash start.sh`, same
variables as above plus the model gateway settings from `config.local.env`).
Under the hood:

- Node comes from a dist tarball (`HARBOR_CC_NODE_DIST_URL`, resolved from
  `QZ_NODE_DIST_URL` or the npmmirror default) downloaded and unpacked inside
  the Sandbox without depending on its package manager;
- `@anthropic-ai/claude-code` (repo-pinned `CLAUDE_CODE_VERSION`) installs
  from `NPM_CONFIG_REGISTRY`, which defaults to npmmirror on qz;
- the agent talks to the SII model gateway through `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` (derived from `BASE_URL` / `API_KEY`); the gateway
  natively serves the Anthropic `/v1/messages` API;
- realtime Opik hooks stay disabled (they need host bind mounts), so either
  leave `OPIK_URL` empty or point it at a remote Opik endpoint (a local Opik
  stack is not reachable from qz runs, same as e2b).

### opencode (via the launcher)

`AGENT=opencode` uses the same launcher, qz Template, and configurable runtime
sources as Claude Code. The generated custom-provider config continues to
route the agent through `BASE_URL` / `API_KEY`.

Start with `OPIK_URL` empty. Traced OpenCode runs require a remote Opik and
a sandbox-reachable Python package mirror for the hook dependencies; they do
not use runner-local bind mounts or the runner-local wheel HTTP server.

### pi (direct `harbor run`)

`qz_pi_agent.py` is a Pi subclass with the same configurable in-Sandbox
runtime sources and a gateway provider injected via `models.json`; the
launcher does not manage pi, so drive Harbor's CLI directly from the runner
environment:

```bash
source Agents/utils/common/Harbor/env.sh

SBX_API_KEY=sbx_xxx QZ_SANDBOX_TEMPLATE=your_template \
BASE_URL=<gateway-url> API_KEY=<gateway-key> \
PYTHONPATH=Agents/utils/common/Harbor \
"$HARBOR_CLI_BIN" run -p <task-dir> -a qz_pi_agent:QzPi -m <model> \
  -e "qz_e2b_sandbox:QzSandboxEnvironment" -n 1 -o jobs -y
```

The `smoke/hello_sandbox` task in this directory is a minimal fixture for
exactly this loop (oracle reward 1.0 in ~6 s, pi reward 1.0 in ~47 s against
`glm` via the SII gateway).

## Limitations

- Tasks must be **single-container and image-backed**. Final-image tasks use
  `environment.docker_image`; environment mappings may additionally put common
  `USER` / `RUN` / `WORKDIR` steps in the Template, execute ordered task init
  commands after each fresh Sandbox is created, and hand off an exact leading
  setup block before agent run. General Dockerfile/build-context and
  docker-compose materialization are not supported.
- `QZ_SANDBOX_TEMPLATE` keeps the backward-compatible fixed mode.
  `QZ_SANDBOX_TEMPLATE_MAP` selects a ready Template per task; the runner
  validates live status but never creates Templates implicitly. Inventory,
  explicit one-task materialization, binding, and cache rules are documented
  in [QZ Template Mapping](QZ_TEMPLATE_MAPPING.md).
- Task network policies (no-network / allowlist) are not verified on qz yet;
  do not rely on network isolation for now.

## Troubleshooting

| Error | Cause and fix |
| --- | --- |
| `template 'xxx' not found` | Name misspelled, or the Template is bound to a different key |
| `Timeout cannot be greater than 4 hours` | Lower `QZ_SANDBOX_TIMEOUT_SEC` to 14400 or below |
| `No available resources` | The pool may be full, or external create traffic may exceed the type group's instantaneous create window. Agent Fleet shapes its own create calls automatically; contact QZ if the error persists. |
| Connection timeout / DNS failure | The machine is not on the SII internal network |
| 401 | The key is invalid or deleted; check the「Sandbox Key」page |

Protocol details and the adapter implementation live in the module docstring
of `qz_e2b_sandbox.py`.
