# Agent Fleet

Agent Fleet provides runnable integrations and benchmark tasksets for
evaluating Claude Code, OpenCode, and OpenClaw.

## Quick Start

### 1. Install prerequisites

- Docker with Docker Compose v2
- Python 3.9 or newer
- `git`, `curl`, and `jq`

Install these with your system package manager. `setup.sh` checks the few
remaining system tools (preinstalled on most distros) and reports anything
missing; everything else is installed automatically.

### 2. Clone the repository

```bash
git clone --recurse-submodules https://github.com/sii-system/agent-fleet.git
cd agent-fleet
```

### 3. Configure and set up

Setup asks for an optional `OPIK_URL`: enter the endpoint to use Opik, or press
Enter to continue without it.

```bash
export BASE_URL=https://your-model-gateway.example.com  # Do not include /v1
export API_KEY=your-api-key
export MODEL=your-model-id

# Optional: pre-fill this or enter it when setup prompts.
# export OPIK_URL=https://your-opik-host/api

./scripts/setup.sh
```

### 4. Run one benchmark

Validate the environment with a one-task canary first:

```bash
MIN_TEST=1 ./scripts/run_fleet.sh \
  --taskset terminalbench21 \
  --agent claude-code \
  --workers 1
```

Then start the full benchmark, with direct arguments or in natural language
(AI mode):

```bash
./scripts/run_fleet.sh --taskset terminalbench21 --agent claude-code --workers 10
./scripts/run_fleet.sh --taskset terminalbench21 --task fix-git --workers 1
./scripts/run_fleet.sh --prompt "Run terminalbench21 with claude-code and 10 workers"
```

The run shows live progress and final results on screen. The first run is
slower while the taskset and Docker images download; rerun `setup.sh` only
when configuration changes.

## FleetSpec runs

A FleetSpec is a small JSON file that declares one benchmark run — taskset,
agent, and worker count — so runs are reproducible and can be launched in
batches. See [scripts/README.md § FleetSpec JSON](./scripts/README.md#fleetspec-json)
for the full format.

```bash
# One saved FleetSpec file
./scripts/run_fleet.sh --spec fleet-spec.json

# Multiple runs launch concurrently: one JSON array file, several files, or both
./scripts/run_fleet.sh --spec run-a.json run-b.json
```

| Flag | Short | Purpose |
| --- | --- | --- |
| `--taskset` | `-t` | Taskset to run ([available tasksets](./scripts/README.md#fleet-launch-modes)) |
| `--task` | — | Exact task name(s), comma-separated or repeated |
| `--agent` | `-a` | `claude-code`, `opencode`, or `openclaw` |
| `--workers` | `-n` | Concurrency |
| `--prompt` | `-p` | Natural-language run request (AI mode) |
| `--spec` | `-s` | FleetSpec file(s) |
| `--output` | `-o` | Save the validated spec |
| `--dry-run` | — | Preview the commands without running |
| `--detach` | `-d` | Detached mode (automatic for multi-run) |

## Docker-in-Docker runs

On hosts where Docker Hub needs registry mirrors, wrap the same arguments with
the Docker-in-Docker launcher instead:

```bash
./scripts/dind-run.sh --taskset terminalbench21 --agent claude-code --workers 1
```

See [scripts/README.md § dind-run.sh](./scripts/README.md#dind-runsh) for
configuration and caveats.

## More details

- Launch modes and limitations:
  [scripts/README.md](./scripts/README.md#current-limitations)
- Tasksets: [Tasks/README.md](./Tasks/README.md)
- Skills: [skills/README.md](./skills/README.md)
- Repository structure: [STRUCT.md](./STRUCT.md)
- Tips and troubleshooting:
  [scripts/README.md](./scripts/README.md#tips--caveats)
- Harbor runner: [Agents/utils/common/Harbor/STRUCT.md](./Agents/utils/common/Harbor/STRUCT.md)
