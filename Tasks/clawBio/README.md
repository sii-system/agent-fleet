# ClawBio Benchmark

Run [ClawBio](https://github.com/ClawBio/ClawBio) bioinformatics skills in parallel across multiple Dockerized [OpenClaw](https://openclaw.ai/) gateway instances.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Workflow](#workflow)
- [Quick Start](#quick-start)
- [Output Structure](#output-structure)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)

---

## Overview

This benchmark suite tests ClawBio skills across multiple OpenClaw instances running in Docker containers. The workflow reuses the generic OpenClaw fleet infrastructure (`Agents/Openclaw/scripts/setup.sh`) and patches the generated configs to load the ClawBio plugin from a shared cache directory — no per-container plugin installation needed.

Each instance is fully isolated with its own:

- Configuration directory (managed by `Agents/Openclaw/scripts/setup.sh`)
- Workspace directory
- Gateway token (`TOKEN_N`)
- Bridge network

The benchmark distributes tasks across instances using round-robin scheduling and collects artifacts for analysis.

---

## Prerequisites

### Required

| Dependency | Version | Description |
|------------|---------|-------------|
| Docker Engine | 20.10+ | Container runtime |
| Docker Compose | v2+ | Container orchestration |
| `jq` | — | JSON processing for config patching |
| OpenClaw image | — | Built via `Agents/Openclaw/scripts/build-openclaw-image.sh` |

### Optional

| Dependency | Purpose |
|------------|---------|
| `git` | Clone ClawBio plugin repository |

### Building the OpenClaw Image

```bash
# From repo root
TRACE_TO_OPIK=false ./Agents/Openclaw/scripts/build-openclaw-image.sh
```

For tracing, initialize the submodule and build with
`TRACE_TO_OPIK=true OPIK_PLUGIN=enabled` instead.

---

## Workflow

### Overall Pipeline

```
1. prewarm-cache.sh          Clone ClawBio plugin to local cache
2. Agents/Openclaw/scripts/setup.sh
                            Generate fleet configs + compose file
3. patch-plugin-config.sh    Patch openclaw.json files to load clawbio
4. docker compose up -d      Start fleet
5. run-benchmark.py          Execute tasks in parallel
```

### Phase 1: prewarm-cache.sh

**Purpose**: Download the ClawBio plugin to a local cache directory.

Clones or updates the ClawBio marketplace repository. No npm install — the plugin is loaded directly via `plugins.load.paths`.

### Phase 2: Agents/Openclaw/scripts/setup.sh

**Purpose**: Generate fleet configs and compose file using the standard OpenClaw setup.

Pass `PLUGIN_CACHE_DIR` to mount the cache directory into containers at `/opt/plugin-cache`.

### Phase 3: patch-plugin-config.sh

**Purpose**: Patch each instance's generated `openclaw.json` to add clawbio plugin config (`plugins.load.paths`, `plugins.allow`, `plugins.entries`) and disable web tools (`tools.deny`).

Must run after `setup.sh` and before `docker compose up`.

### Phase 4: docker compose up

Start the fleet.

### Phase 5: run-benchmark.py

**Purpose**: Execute skill tasks in parallel and collect results.

Discovers instances from `Agents/Openclaw/.env` and workspace paths from `docker inspect`.

---

## Quick Start

### Step 0: Prewarm Cache

Download the ClawBio plugin to a local cache directory:

```bash
./Tasks/clawBio/scripts/prewarm-cache.sh
```

> **Note:** Default cache location is `Tasks/clawBio/cache/`. Use `--cache-dir` or `CACHE_DIR` env var to override.

**Generated files:**
```
cache/clawbio/          # ClawBio plugin repository
```

### Step 1: Setup Fleet

Generate and start the OpenClaw fleet with plugin cache mounted:

```bash
# From repo root
set -a
. ./Tasks/clawBio/config/benchmark.env
set +a
PLUGIN_CACHE_DIR=$(pwd)/Tasks/clawBio/cache \
TRACE_TO_OPIK=false \
BASE_URL="https://api.example.com/v1" \
API_KEY="sk-xxx" \
MODEL="your-model" \
./Agents/Openclaw/scripts/setup.sh 4
```

> **Required:** `BASE_URL` and `API_KEY` must be set via the repo-root `config.env`, environment variables, or CLI. See [Configuration Reference](#configuration-reference) for all options.
>
> **Security:** `config/benchmark.env` is permissive. Source it only for a
> dedicated ClawBio fleet, then stop or regenerate that fleet before other use.

### Step 2: Patch Plugin Config

Patch the generated `openclaw.json` files to load clawbio and disable web tools:

```bash
./Tasks/clawBio/scripts/patch-plugin-config.sh
```

> If using a custom `CONFIG_BASE`, pass it via `--config-base` or `CONFIG_BASE` env var.

### Step 3: Start Fleet

```bash
docker compose -f Agents/Openclaw/docker-compose.yml up -d
```

**Container ports:**
- Instance 1: `18789`
- Instance 2: `18809`
- Instance N: `18789 + (N-1) * 20`

### Step 4: Run Benchmark

Execute the benchmark tasks:

```bash
# One-command launcher (setup + patch + start + benchmark)
./Tasks/clawBio/scripts/run-openclaw-clawbio.sh

# Optional: override instance count / iteration count at runtime
COUNT=20 ITERATIONS=3 ./Tasks/clawBio/scripts/run-openclaw-clawbio.sh

# Run selected exact task IDs only
./Tasks/clawBio/scripts/run-openclaw-clawbio.sh \
  --tasks rnaseq-de-demo,fine-mapping-demo

# Direct run-benchmark.py is for advanced/manual workflow when fleet is already
# prepared and started by you (Step 1~3), and you only want to execute tasks.
# Do not run it again immediately after the unified launcher unless you
# intentionally want an additional benchmark run.
# From repo root
./Tasks/clawBio/scripts/run-benchmark.py --instances 4

# With custom config
./Tasks/clawBio/scripts/run-benchmark.py --instances 4 --config config/tasks.json

# Select exact task IDs when the fleet is already running
./Tasks/clawBio/scripts/run-benchmark.py --instances 4 \
  --tasks rnaseq-de-demo,fine-mapping-demo

# Run 5 iterations and print per-iteration status summary
./Tasks/clawBio/scripts/run-benchmark.py --instances 4 -n 5

# Skip preflight checks (faster startup)
./Tasks/clawBio/scripts/run-benchmark.py --instances 4 --skip-preflight
```

### Unified Launcher Notes

`run-openclaw-clawbio.sh` validates explicit task IDs before it builds an
image, prepares caches, changes fleet configuration, or restarts containers.
It also validates the ClawBio benchmark security settings before creating run
directories or making any of those changes. It then prewarms the cache,
generates and patches fleet configs, starts the fleet, and invokes
`run-benchmark.py`. Run with `-h` to see all environment variables. Variable
precedence: runtime env →
`Tasks/clawBio/config/benchmark.env` → `Agents/Openclaw/config/fleet.env` →
`config.local.env` → `config.env` → script defaults.

Output layout is described under [Output Structure](#output-structure).

### ClawBio Benchmark Security

ClawBio needs to load its skill from the mounted plugin cache, outside the
instance workspace, and execute unattended shell, Python, and R commands. The
unified launcher therefore loads a dedicated benchmark configuration:

```bash
./Tasks/clawBio/scripts/run-openclaw-clawbio.sh
```

The committed, public-safe `config/benchmark.env` supplies
`SANDBOX_MODE=off`, `EXEC_SECURITY=full`, `EXEC_ASK=off`, and
`WORKSPACE_ONLY=false`. The launcher warns before applying this permissive
profile to the generated fleet. Runtime environment variables still take
precedence; if they conflict, the launcher lists every incompatible setting and
exits before creating the run root, building images, preparing caches, or
changing the fleet.

The launcher does not change the general OpenClaw defaults, but `setup.sh`
writes this profile into the run-specific generated configs and leaves that
fleet running after the benchmark. Stop it before using OpenClaw for another
purpose, or regenerate the fleet with the desired restrictive settings. The
container root filesystem remains read-only by default; ClawBio writes through
its dedicated state and workspace mounts plus the `/tmp` tmpfs.

```bash
docker compose -f Agents/Openclaw/docker-compose.yml down
```

---

## Output Structure

### Results Directory Tree

```
results/
├── latest -> 20260413-120000/     # Symlink to most recent benchmark run root
└── 20260413-120000/               # Timestamped run root
    ├── run.log                    # Cross-iteration log
    ├── iterations-summary.json
    ├── iterations-summary.md
    ├── iteration-001/
    │   ├── results.json
    │   ├── results.md
    │   └── instances/
    │       └── 1/
    │           └── pharmgx-reporter-demo/
    │               ├── task.log
    │               ├── agent-response.json
    │               ├── session-snapshot.json
    │               ├── container.log
    │               ├── metadata.json
    │               └── artifacts/
    └── iteration-002/
```

### Key Output Files

| File | Description |
|------|-------------|
| `iterations-summary.json` | One record per iteration (success/failure counts, success rate, wall time, failure stages) |
| `iterations-summary.md` | Human-readable per-iteration overview |
| `iteration-XXX/results.json` | Complete structured results for one iteration |
| `iteration-XXX/results.md` | Human-readable summary for one iteration |
| `run.log` | Timestamped cross-iteration execution log |
| `instances/N/task-id/task.log` | OpenClaw agent command and output |
| `instances/N/task-id/agent-response.json` | Parsed agent response |
| `instances/N/task-id/container.log` | Docker container logs |
| `instances/N/task-id/artifacts/` | Skill output files (reports, data, etc.) |

---

## Configuration Reference

### prewarm-cache.sh

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_DIR` | `Tasks/clawBio/cache` | Cache root directory |
| `MARKETPLACE_URL` | `https://github.com/ClawBio/ClawBio.git` | Plugin repository URL |
| `PLUGIN_NAME` | `clawbio` | Plugin name for directory |

Flags: `--cache-dir PATH`, `--marketplace-url URL`, `-h/--help`

### patch-plugin-config.sh

Patches each instance's generated `openclaw.json` to add the ClawBio plugin and disable web tools. Must run after `Agents/Openclaw/scripts/setup.sh` and before `docker compose up`.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_BASE` | `$HOME/openclaw-instances` | Per-instance config root |
| `PLUGIN_NAME` | `clawbio` | Plugin directory name under `/opt/plugin-cache` |

Flags: `--config-base DIR`, `--plugin-name NAME`, `-h/--help`

### run-benchmark.py

Discovers instances from `Agents/Openclaw/.env` and workspace paths from `docker inspect`.

```
usage: run-benchmark.py [-h] [--instances N] [--config PATH] [--tasks ID[,ID...]] [--output-dir DIR] [--skip-preflight] [-n N]

Options:
  --instances N       Number of instances to use (default: all discovered)
  --config PATH       Task config file (default: config/tasks.json)
  --tasks ID[,ID...]  Run only the listed exact task IDs
  --output-dir DIR    Output root (default: results/)
  --skip-preflight    Skip container health checks
  -n N, --iterations N
                      Number of benchmark iterations to run (default: 1)
```

### Task Configuration (tasks.json)

```json
{
  "defaults": {
    "timeout_seconds": 1800,
    "thinking": "medium",
    "artifact_paths": ["outputs"]
  },
  "tasks": [
    {
      "id": "task-id",
      "skill": "skill-name",
      "prompt": "Task description...",
      "timeout_seconds": 1200,
      "thinking": "high",
      "artifact_paths": ["outputs"]
    }
  ]
}
```

#### Task Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique task identifier |
| `skill` | No | ClawBio skill name (metadata for logging) |
| `prompt` | Yes | Task description/instructions |
| `timeout_seconds` | No | Task timeout (default: 1800) |
| `thinking` | No | Thinking level: `low`, `medium`, `high` |
| `artifact_paths` | No | Paths to collect (relative to workspace) |

#### Default Fields

| Field | Description |
|-------|-------------|
| `timeout_seconds` | Default timeout for all tasks |
| `thinking` | Default thinking level |
| `artifact_paths` | Default paths to collect from workspace |

---

## Troubleshooting

### Container not healthy

```bash
# Check container logs
docker compose -f Agents/Openclaw/docker-compose.yml logs openclaw-1

# Check health status
docker inspect --format='{{.State.Health.Status}}' openclaw-1
```

### Cache not found

Run prewarm-cache.sh before setup:

```bash
./Tasks/clawBio/scripts/prewarm-cache.sh
```

### Sandbox errors blocking skill execution

The unified launcher now rejects incompatible settings before fleet setup. If
you use the manual setup flow and see errors such as "path escapes sandbox",
`workspaceOnly`, or `exec denied`, apply all settings from
[ClawBio Benchmark Security](#clawbio-benchmark-security), regenerate the
fleet, and restart it.

### Artifacts directory is empty

Ensure task prompts specify output to the `outputs/` directory:

```json
"prompt": "Generate a report. Output to outputs/report/"
```

And `artifact_paths` is configured correctly:

```json
"artifact_paths": ["outputs"]
```

---

## Fleet Management

Use `Agents/Openclaw/scripts/openclaw-fleet.sh` for status, logs, start/stop/restart, and per-instance ops. See [`Agents/Openclaw/README.md`](../../Agents/Openclaw/README.md#openclaw-fleetsh).
