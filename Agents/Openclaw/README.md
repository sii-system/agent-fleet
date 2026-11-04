# OpenClaw-Fleet

Run multiple [OpenClaw](https://openclaw.ai/) gateway instances on a single host using Docker Compose.

## Architecture

Each instance is a Docker container with its own config, workspace, gateway token, port, and isolated bridge network — instances are fully independent and cannot communicate with each other.

```
┌─────────────────────── Host (macOS / Linux) ───────────────────────┐
│                                                                    │
│  ┌─────────────────┐  ┌─────────────────┐       ┌─────────────────┐│
│  │   openclaw-1    │  │   openclaw-2    │  ...  │   openclaw-N    ││
│  │                 │  │                 │       │                 ││
│  │  port :18789    │  │  port :18809    │       │  port :18789    ││
│  │                 │  │                 │       │  + (N-1)*20     ││
│  │  config/1/      │  │  config/2/      │       │  config/N/      ││
│  │  workspace/1/   │  │  workspace/2/   │       │  workspace/N/   ││
│  │  token: TOKEN_1 │  │  token: TOKEN_2 │       │  token: TOKEN_N ││
│  └────────┬────────┘  └────────┬────────┘       └────────┬────────┘│
│           │                    │                         │         │
│      [bridge net 1]      [bridge net 2]           [bridge net N]   │
│       (isolated)          (isolated)               (isolated)      │
└────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Docker Engine + Docker Compose v2** — [Install Docker Engine](https://docs.docker.com/engine/install/) or [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- **`git`**, **`docker`**, **`npm`** — for building the openclaw Docker image (pinned to OpenClaw `v2026.5.10-beta.1` in `build-openclaw-image.sh`)
- **`openssl`**, **`python3`** (>= 3.9) — pre-installed on macOS and most Linux distros

## Quick Start

```bash
# Build the Docker image
./Agents/Openclaw/scripts/build-openclaw-image.sh

# Generate 3 instances with your model provider
BASE_URL="https://api.example.com/v1" API_KEY="sk-xxx" MODEL="nex/nex-n1.1" ./Agents/Openclaw/scripts/setup.sh 3

# Launch
docker compose -f Agents/Openclaw/docker-compose.yml up -d

# Check health
./Agents/Openclaw/scripts/openclaw-fleet.sh status
```

`setup.sh` exits without generating the fleet if `BASE_URL` or `API_KEY` is missing. Tokens and ports are assigned automatically.

## Common Commands

```bash
./Agents/Openclaw/scripts/openclaw-fleet.sh status               # Health, CPU/mem, ports
./Agents/Openclaw/scripts/openclaw-fleet.sh logs all --tail 100  # Tail logs across fleet
./Agents/Openclaw/scripts/openclaw-fleet.sh restart 1,3          # Restart selected instances
./Agents/Openclaw/scripts/openclaw-fleet.sh scale 5              # Resize fleet
```

Selectors accept `all`, `3`, `1,3,5`, or `2-5`. See [GUIDE.md](./GUIDE.md#fleet-management) for the full command list and Session TUI.

## Generated Files

- `Agents/Openclaw/docker-compose.yml` — generated Compose file; do not edit manually
- `Agents/Openclaw/.env` — generated tokens (`TOKEN_1` … `TOKEN_N`)
- `$CONFIG_BASE/N/openclaw.json` — per-instance OpenClaw config
- `$WORKSPACE_BASE/N/` — per-instance workspace

## Documentation

| Doc | Contents |
|---|---|
| [GUIDE.md](./GUIDE.md) | Configuration variables, opik plugin, package mirrors, `openclaw.json.template`, `openclaw-fleet.sh` command reference, Session TUI, Linux/macOS notes, files layout |
| [SECURITY.md](./SECURITY.md) | Container/image/config/infra policy matrix, known limitations |

## Benchmarks

To run PinchBench against the fleet, see [`Tasks/Pinchbench/`](../../Tasks/Pinchbench/).
