# OpenClaw Guide

Operator-facing reference: configuration, fleet commands, multi-node deployment, and platform notes. See [SECURITY.md](./SECURITY.md) for the security policy matrix.

## Configuration

`setup.sh` reads configuration from multiple sources (highest precedence first):

1. Command-line flags and positional `COUNT`
2. Environment variables set by the caller
3. `Agents/Openclaw/config/fleet.env` — OpenClaw config
4. `config.local.env` — private overrides/secrets
5. `config.env` — shared site configuration (model gateway, Opik, mirrors)

Set your model gateway in the repo-root `config.env`, then edit `Agents/Openclaw/config/fleet.env` for fleet-specific options:

```bash
# Set BASE_URL, API_KEY, MODEL in config.env
# Set COUNT and other OpenClaw options in Agents/Openclaw/config/fleet.env
./Agents/Openclaw/scripts/setup.sh
```

Or use environment variables directly:

```bash
BASE_URL="https://api.example.com/v1" API_KEY="sk-xxx" ./Agents/Openclaw/scripts/setup.sh 3
```

Or override generated runtime settings directly:

```bash
BASE_URL="https://api.example.com/v1" \
API_KEY="sk-xxx" \
CONTAINER_NAME_PREFIX="lab" \
DEFAULT_PORTS_OFFSET="100" \
./Agents/Openclaw/scripts/setup.sh 3 \
  --sandbox_mode workspace-write \
  --exec_security allow \
  --exec_ask never \
  --docker_compose_read_only false
```

### Variables

| Variable | Default | Description |
|---|---|---|
| `COUNT` | `2` | Number of instances (or pass as argument) |
| `BASE_URL` | _(none)_ | Model provider base URL |
| `API_KEY` | _(none)_ | Model provider API key |
| `MODEL` | `default-model` | Model identifier |
| `CONFIG_BASE` | `$HOME/openclaw-instances` | Config/state root mounted at `/home/node/openclaw-state` and exposed through `OPENCLAW_STATE_DIR` / `OPENCLAW_CONFIG_PATH` |
| `WORKSPACE_BASE` | `$HOME/openclaw-workspaces` | Workspace root mounted at `/home/node/workspace` |
| `NPM_CACHE_DIR` | `$HOME/.npm` | npm cache directory mounted into containers |
| `PLUGIN_CACHE_DIR` | _(empty)_ | Optional plugin cache directory mounted into containers |
| `OPENCLAW_UID` | _(current uid)_ | UID for ownership of generated config/workspace dirs |
| `OPENCLAW_GID` | _(current gid)_ | GID for ownership of generated config/workspace dirs |
| `OPENCLAW_CONTAINER_USER` | _(empty)_ | Optional `user:` override in generated Compose (e.g. `1000`) |
| `OPENCLAW_CONFIG_CHMOD` | `a+rwX` | `chmod` spec applied to generated config dirs |
| `OPENCLAW_CONFIG_DEFAULT_ACL` | `true` | Apply default ACLs to config dirs (Linux only) |
| `OPENCLAW_WORKSPACE_CHMOD` | `a+rwX` | `chmod` spec applied to generated workspace dirs |
| `OPENCLAW_WORKSPACE_DEFAULT_ACL` | `true` | Apply default ACLs to workspace dirs (Linux only) |
| `NPM_CONFIG_REGISTRY` | _(empty)_ | npm/pnpm registry mirror passed into generated containers and honored by `build-openclaw-image.sh` |
| `PIP_INDEX_URL` | _(empty)_ | pip index mirror passed into generated containers and the Opik image build |
| `PIP_EXTRA_INDEX_URL` | _(empty)_ | Optional extra pip index URL passed into generated containers and the Opik image build |
| `PIP_TRUSTED_HOST` | _(empty)_ | Optional pip trusted host passed into generated containers and the Opik image build |
| `CONTAINER_NAME_PREFIX` | `openclaw` | Prefix used for generated service, container, and network names |
| `CPU_LIMIT` | `2` | CPU limit per container |
| `MEM_LIMIT` | `4G` | Memory limit per container |
| `PORT_STEP` | `20` | Port gap between instances |
| `DEFAULT_PORTS_OFFSET` | `0` | Offset added to the base host gateway port `18789` |
| `SANDBOX_MODE` | `off` | Default value for `agents.defaults.sandbox.mode` |
| `HEARTBEAT_EVERY` | `0m` | Default value for `agents.defaults.heartbeat.every`; `0m` disables heartbeat, set a positive cadence like `30m` to enable it |
| `EXEC_SECURITY` | `deny` | Default value for `tools.exec.security` |
| `EXEC_ASK` | `always` | Default value for `tools.exec.ask` |
| `WORKSPACE_ONLY` | `true` | Default value for `tools.fs.workspaceOnly`; set `false` to let skills read outside the workspace (e.g. plugin/extension dirs) |
| `DOCKER_COMPOSE_READ_ONLY` | `true` | Default value for generated Compose `read_only` |
| `OPENCLAW_IMAGE` | `openclaw:local` | Docker image to use |
| `TRACE_TO_OPIK` | `true` | Fleet-wide tracing switch; `false` forces the OpenClaw plugin off |
| `OPIK_PLUGIN` | `disabled` | Set to `enabled` to activate opik tracing |
| `OPIK_URL` | _(none)_ | Opik API endpoint (required when `OPIK_PLUGIN=enabled`) |
| `OPIK_PROJECT_NAME` | _(none)_ | Opik project name (required when `OPIK_PLUGIN=enabled`) |
| `OPIK_API_KEY` | _(empty)_ | Opik API key |
| `OPIK_WORKSPACE` | `default` | Opik workspace name |
| `TZ` | `Asia/Shanghai` | Container timezone |

`setup.sh` also accepts `--sandbox_mode`, `--exec_security`, `--exec_ask`, and `--docker_compose_read_only`, with defaults matching the values above.

Existing tokens in `Agents/Openclaw/.env` are preserved across regenerations. Generated service names follow `${CONTAINER_NAME_PREFIX}-N`. Host gateway port assignment follows `18789 + DEFAULT_PORTS_OFFSET + (N-1) * PORT_STEP`, per [gateway docs](https://docs.openclaw.ai/gateway/multiple-gateways).

The generated containers intentionally do not mount over `/home/node/.openclaw`. OpenClaw state is redirected with `OPENCLAW_STATE_DIR=/home/node/openclaw-state` and `OPENCLAW_CONFIG_PATH=/home/node/openclaw-state/openclaw.json`, which keeps image-provided or runtime-created `.openclaw` content from being hidden by a host bind mount.

### Opik Tracing Plugin

Build with `OPIK_PLUGIN=enabled` and provide the opik config at setup:

```bash
OPIK_PLUGIN=enabled ./Agents/Openclaw/scripts/build-openclaw-image.sh

OPIK_PLUGIN=enabled \
OPIK_URL="https://opik.example.com/api/" \
OPIK_PROJECT_NAME="my-project" \
BASE_URL="https://api.example.com/v1" API_KEY="sk-xxx" MODEL="nex/nex-n1.1" \
./Agents/Openclaw/scripts/setup.sh 3
```

`TRACE_TO_OPIK=false` is authoritative: shared setup and image builds ignore a
stale `OPIK_PLUGIN=enabled` value and use `openclaw:local` without requiring an
Opik endpoint or plugin checkout. Both scripts read `config.env`,
`config.local.env`, and `config/fleet.env`; one-off caller environment values
retain the highest precedence.

### Package Mirrors

For restricted networks, pass npm and pip mirrors to the build and generated containers with standard package-manager env vars:

```bash
NPM_CONFIG_REGISTRY="https://registry.npmmirror.com" \
PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple" \
PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn" \
OPIK_PLUGIN=enabled ./Agents/Openclaw/scripts/build-openclaw-image.sh

NPM_CONFIG_REGISTRY="https://registry.npmmirror.com" \
PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple" \
PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn" \
BASE_URL="https://api.example.com/v1" API_KEY="sk-xxx" MODEL="nex/nex-n1.1" \
./Agents/Openclaw/scripts/setup.sh 3
```

### openclaw.json.template

Base config template. `setup.sh` automatically injects per-instance token, port, and allowed origins — you don't need to set those manually.

The default template disables OpenClaw heartbeats with `agents.defaults.heartbeat.every = "0m"` so benchmark fleets do not spend tokens or create background heartbeat sessions. Set `HEARTBEAT_EVERY` to a positive cadence such as `30m` before running `setup.sh` if a fleet needs periodic heartbeat behavior.

Placeholders:
- `{{BASE_URL}}` — model provider base URL
- `{{API_KEY}}` — model provider API key
- `{{MODEL}}` — model identifier
- `{{SANDBOX_MODE}}` — default agent sandbox mode
- `{{HEARTBEAT_EVERY}}` — default heartbeat cadence (`0m` disables heartbeat)
- `{{EXEC_SECURITY}}` — exec tool security policy
- `{{EXEC_ASK}}` — exec tool approval policy
- `{{WORKSPACE_DIR}}` — in-container workspace path (auto-set to `/home/node/workspace`)

Edit this file to customize agents, tools, plugins, or other settings for all instances.

## Fleet Management

### openclaw-fleet.sh

Fleet management tool. All commands accept a **selector**: `all`, `3`, `1,3,5`, or `2-5`.

```bash
./Agents/Openclaw/scripts/openclaw-fleet.sh status  [sel]                  # Health, CPU/mem, ports
./Agents/Openclaw/scripts/openclaw-fleet.sh probe   [sel]                  # Authenticated WebSocket probe
./Agents/Openclaw/scripts/openclaw-fleet.sh logs    [sel] [--tail N] [-f]  # Container logs
./Agents/Openclaw/scripts/openclaw-fleet.sh start   [sel]                  # Start containers
./Agents/Openclaw/scripts/openclaw-fleet.sh stop    [sel]                  # Stop containers
./Agents/Openclaw/scripts/openclaw-fleet.sh restart [sel]                  # Restart containers
./Agents/Openclaw/scripts/openclaw-fleet.sh token   [sel]                  # Show gateway tokens
./Agents/Openclaw/scripts/openclaw-fleet.sh config  <N>                    # Show config for instance N
./Agents/Openclaw/scripts/openclaw-fleet.sh config-set <sel> '<jq-expr>'   # Bulk-edit configs (requires jq)
./Agents/Openclaw/scripts/openclaw-fleet.sh exec    <N> [cmd...]           # Exec into container
./Agents/Openclaw/scripts/openclaw-fleet.sh workspace [sel]                # Workspace disk usage
./Agents/Openclaw/scripts/openclaw-fleet.sh clean-workspace <sel>          # Wipe workspace contents
./Agents/Openclaw/scripts/openclaw-fleet.sh scale   <N>                    # Resize fleet (down + regenerate + up)
./Agents/Openclaw/scripts/openclaw-fleet.sh plugin-status [sel]            # Show installed plugins per instance
./Agents/Openclaw/scripts/openclaw-fleet.sh df                             # Disk overview
```

Sample output of `status`:

```
INSTANCE       HEALTH   CPU        MEM                MEM%   PORT
──────────────────────────────────────────────────────────────────────
openclaw-1     live     0.04%      252.8MiB / 7.652GiB 3.23%  :18789
openclaw-2     live     0.05%      250.7MiB / 7.652GiB 3.20%  :18809
openclaw-3     live     0.03%      249.1MiB / 7.652GiB 3.18%  :18829
```

If you change `CONTAINER_NAME_PREFIX` or `DEFAULT_PORTS_OFFSET`, `openclaw-fleet.sh` follows those values automatically.

### Session TUI

Launch a Zellij dashboard with an overview tab plus one pane per `openclaw-N` instance:

```bash
./Agents/Openclaw/scripts/start-session-tui.sh
```

The layout reuses the same grid style as `Agents/Terminal-bench-tui`. The first tab includes:

- one monitor pane with fleet-level active/idle counts and the currently active sessions
- five lightweight session panes for instances `1-5`

Additional tabs render the remaining worker panes in `10`-pane batches.

Each session pane stays intentionally compact and shows the most relevant non-heartbeat session for its instance. It renders:

- instance + gateway port
- session id + status
- target
- latest turn summary

Implementation notes:

- The monitor and pane renderers read session state from the matching `openclaw-N` container.
- If no normal session is visible, the pane shows an idle placeholder and keeps polling.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_TUI_RUNTIME_DIR` | `Agents/Openclaw/.runtime` | Directory for generated layout files |
| `OPENCLAW_SESSION_POLL_INTERVAL` | `2` | Seconds between pane refreshes |
| `ZELLIJ_BIN` | `zellij` | Zellij executable override |

## Deployment

### Multi-Node (Ansible)

To deploy the fleet across multiple nodes, use the bundled Ansible playbook. It clones the repo on each node, renders `fleet.env` from a template, runs `setup.sh`, and starts the fleet.

```bash
# Edit inventory.ini with your node IPs first
ansible-playbook -i Agents/Openclaw/config/ansible/inventory.ini \
  Agents/Openclaw/config/ansible/deploy.yml \
  -e base_url="https://api.example.com/v1" \
  -e api_key="sk-xxx" \
  -e model="nex/nex-n1.1" \
  -e trace_to_opik=false \
  -e openclaw_image="registry.example.com/openclaw:latest"
```

The playbook renders `Agents/Openclaw/config/ansible/templates/fleet.env.j2` into `Agents/Openclaw/config/fleet.env` on each node, then runs `setup.sh` and `docker compose up -d`.
To enable tracing, set `trace_to_opik=true`, `opik_plugin=enabled`,
`opik_url`, and `opik_project_name`.

### Linux Notes

- Prefix docker commands with `sudo` if your user isn't in the `docker` group
- `setup.sh` writes generated files first, then runs `chown` on config/workspace dirs for `OPENCLAW_UID:OPENCLAW_GID` (defaults to the current user/group) on Linux
- Swap limit warnings on older kernels (5.4) are cosmetic — memory limits still work
- Config/state and workspace mounts default to `OPENCLAW_CONFIG_CHMOD=a+rwX` / `OPENCLAW_WORKSPACE_CHMOD=a+rwX` for benchmark runners; see [SECURITY.md](./SECURITY.md) for the host-permission tradeoff

### macOS Notes

- Docker Desktop runs containers in a Linux VM (extra isolation layer)
- Use `curl -x '' http://127.0.0.1:PORT/healthz` if a local proxy intercepts localhost
- Containers survive sleep; `restart: unless-stopped` handles wake recovery

## Files Layout

```
agent-fleet/
├── Agents/
│   └── Openclaw/
│       ├── Dockerfile.opik                # Derived image with opik-tracer plugin
│       ├── scripts/
│       │   ├── setup.sh                    # Generates docker-compose.yml + per-instance configs
│       │   ├── build-openclaw-image.sh     # Build openclaw:local (or openclaw:local-opik)
│       │   └── openclaw-fleet.sh           # Fleet management tool
│       ├── config/
│       │   ├── ansible/
│       │   │   ├── deploy.yml              # Multi-node deployment playbook
│       │   │   ├── inventory.ini           # Node inventory (edit with your IPs)
│       │   │   └── templates/fleet.env.j2  # fleet.env template rendered per node
│       │   ├── openclaw.json.template      # Base config template
│       │   └── fleet.env                   # Local setup defaults
│       ├── docker-compose.yml              # Auto-generated — do not edit manually
│       ├── .env                            # Auto-generated tokens (TOKEN_1 … TOKEN_N)
│       ├── README.md                       # Quick start + architecture
│       ├── GUIDE.md                        # Configuration, fleet commands, deployment
│       └── SECURITY.md                     # Policy matrix + known limitations
└── README.md
```
