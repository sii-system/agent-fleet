# AGENTS.md — Tasks/

Benchmark task inputs, Harbor task generators, and OpenClaw benchmark runners.
Harbor task lists are consumed by the shared runner in
`Agents/utils/common/Harbor/`; PinchBench and ClawBio run against an OpenClaw
fleet from `Agents/Openclaw/` (see
[Agents/AGENTS.md](../Agents/AGENTS.md)). Repo-wide config rules:
[root AGENTS.md](../AGENTS.md).

## Layout

| Path | Role |
| --- | --- |
| `SETA/`, `SWE-smith/`, `SWE-verify/`, `Terminal-bench-2/` | Harbor task lists |
| `SWE-rebench-v2/` | Official SWE-rebench-V2 native Harbor task generator |
| `SWE-rebench-v2-TaskTrove/` | Third-party TaskTrove Harbor registry integration |
| `TMax/` | Harbor registry dataset entrypoint |
| `BrowseComp/`, `DeepSearchQA/` | Native Harbor dataset entrypoints; automatic preparation and optional manual adapters share `Agents/utils/web_search/` |
| `Pinchbench/` | PinchBench runner for the OpenClaw fleet |
| `clawBio/` | ClawBio bioinformatics benchmark for the OpenClaw fleet |
| `WindowsAgentArena/` | WAA / WAA-V2 Harbor task adapters and native agent/verifier integration |

## Harbor Task Lists

These lists are selected for local runs, including `DATASET_NAME=auto` with a
matching `DATASET_PATH`:

- `seta` → `SETA/harbor_tasks.txt`
- `smith` → `SWE-smith/harbor_tasks.txt`
- `sweverify` → `SWE-verify/harbor_tasks.txt`
- `terminalbench21` → `Terminal-bench-2/harbor_terminalbench21_tasks.txt`

The `seta`, `sweverify`, and `terminalbench21` aliases resolve to Harbor
registry datasets by default and skip these local files. `TASK_SOURCE_FILE=<path>`
overrides the built-in selection for local runs. Task lists are owned here —
don't duplicate them under `Agents/`.

Other registry datasets use their full IDs, including
`tmax/TMax-15K-Harbor`. The unified launcher accepts these via
`./scripts/run_fleet.sh --taskset <registry-id>`, or an explicit local dataset
path.

## SWE-rebench-V2 Adapter (`SWE-rebench-v2/`)

Generates native Harbor tasks from the official 32,079-row SWE-rebench-V2
Parquet dataset. Environment setup belongs in the generated Dockerfile and
must never be added to the agent instruction. The top-level upstream final
image is metadata only; environments are rebuilt from the record's builder
base image, repository, base commit, and install commands.

Keep generated tasks outside the repository. Use
`agent-fleet-swe-rebench-v2` as the local taskset and verifier benchmark name
so the existing portable runtime bundle is selected on every supported
backend. Conversion,
prebuild, and run commands are in
[SWE-rebench-v2/README.md](SWE-rebench-v2/README.md). The previous TaskTrove
registry integration remains documented under
[SWE-rebench-v2-TaskTrove/](SWE-rebench-v2-TaskTrove/) as an explicitly
third-party option.

## Web Search Tasks (`BrowseComp/`, `DeepSearchQA/`)

With `HARBOR_CC_WEB_MCP_ENABLED=1`, selecting `DATASET_NAME=browsecomp` or
`deepsearchqa` automatically prepares a missing dataset before running tasks.
No manual `uv` generation step is required. After normal host setup and
model/sandbox configuration, run from the repository root:

```bash
HARBOR_CC_WEB_MCP_ENABLED=1 OPIK_URL= \
  ./scripts/run_fleet.sh --taskset browsecomp --agent opencode --workers 1
```

Use `--taskset deepsearchqa` for DeepSearchQA. The default output directory is
`$AGENT_FLEET_CACHE_DIR/web-search/tasks/$DATASET_NAME`; override it with
`DATASET_PATH`. Existing directories are reused unchanged. If missing,
startup downloads or reuses the cached official CSV, calls the shared adapter,
then continues the normal Agent Fleet flow. Conversion never runs inside a trial.

**Optional manual preparation** from official CSV files:

```bash
uv run --project Agents/utils/web_search python Tasks/BrowseComp/adapter.py --input /data/browse_comp_test_set.csv --output-dir /data/harbor/browsecomp
uv run --project Agents/utils/web_search python Tasks/DeepSearchQA/adapter.py --input /data/DSQA-full.csv --output-dir /data/harbor/deepsearchqa
```

Source count and SHA-256 validation happen before task filtering; keep each
dataset's validation and reward semantics separate. Edit the shared generator
and `Agents/utils/web_search/src/web_search_adapter/task-template/` to change
generated tasks.

For rollout, `ROLLOUT=1 DATASET_NAME=browsecomp` (or `deepsearchqa`) with the
same MCP switch also prepares the primary dataset before the listener starts.
Register additional already-prepared roots in `RL_DATASET_ROOTS`. The verifier
reuses the trial model gateway and returns rewards through the existing
rollout path; do not configure `RL_RESULT_PROCESSOR`. Configuration and
generation options: [web_search/README.md](../Agents/utils/web_search/README.md).

## PinchBench (`Pinchbench/`)

Shards PinchBench tasks across OpenClaw gateway instances (one Docker
worker per gateway) and merges results.

Prereq: a running OpenClaw fleet ([Agents/AGENTS.md](../Agents/AGENTS.md)).

```bash
$EDITOR Tasks/Pinchbench/config/pinchbench.env   # usually only MODEL needed
API_KEY="$PROVIDER_API_KEY" ./Tasks/Pinchbench/scripts/run-parallel-workers.py --instances 3
# multiple iterations:
API_KEY="$PROVIDER_API_KEY" ./Tasks/Pinchbench/scripts/run-parallel-workers.py --instances 3 -n 5
```

Sanity-check a new setup first:

```bash
API_KEY="$PROVIDER_API_KEY" ./Tasks/Pinchbench/scripts/run-parallel-workers.py \
  --instances 1 --suite task_sanity
```

For local OpenAI-compatible backends (vLLM/SGLang), also set
`PINCHBENCH_MODEL_PROVIDER` in `pinchbench.env`. The runner clones the
pinned upstream PinchBench into `/tmp/pinchbench-skill` and applies
`Tasks/Pinchbench/patches/`.

Outputs: `Tasks/Pinchbench/.pinchbench-results-docker/<timestamp>/`
(`iteration-NNN/parallel-merged.json`, `iterations-summary.{json,md}`).
Terminal summary of a merged file (defaults to the latest run):

```bash
python3 Tasks/Pinchbench/scripts/summary.py
```

Full configuration table and worker/gateway mount details:
[Pinchbench/README.md](Pinchbench/README.md).

## ClawBio (`clawBio/`)

Five-phase pipeline (prewarm plugin cache → fleet `setup.sh` with
`PLUGIN_CACHE_DIR` → `patch-plugin-config.sh` → `docker compose up -d` →
`run-benchmark.py`). The unified launcher runs all of it:

```bash
./Tasks/clawBio/scripts/run-openclaw-clawbio.sh
COUNT=20 ITERATIONS=3 \
  ./Tasks/clawBio/scripts/run-openclaw-clawbio.sh
```

`patch-plugin-config.sh` must run after `setup.sh` and before
`docker compose up`. The phase-by-phase manual flow with the full
environment-variable example: [clawBio/README.md](clawBio/README.md).
The launcher loads and warns about the dedicated execution profile in
`clawBio/config/benchmark.env`.
The unified launcher leaves the benchmark fleet running with its run-specific
execution profile; stop or regenerate it before using OpenClaw for another
purpose.

Outputs: `Tasks/clawBio/results/latest/` →
`iterations-summary.{json,md}`, `iteration-NNN/results.{json,md}`, and
per-task logs/artifacts under `instances/<N>/<task-id>/`.

Sandbox errors ("path escapes sandbox"): rerun `setup.sh` with
`WORKSPACE_ONLY=false`.

## Development

WindowsAgentArena uses its own host environment and pinned native upstream clients.
Run `WindowsAgentArena/setup.sh` explicitly; startup must only validate it.
Harbor owns trial orchestration, retries and resume. Task materialization and native setup/evaluation belong here; host agents belong under
`Agents/WindowsAgentArena/`. Reuse the shared Windows KubeVirt backend for VM
ownership and cleanup. See [WindowsAgentArena/README.md](WindowsAgentArena/README.md)
for full runs, resume and pinned-client integration tests.

Run from the repo root:

```bash
python3 -m unittest discover -s Tasks/Pinchbench/tests
python3 -m unittest discover -s Tasks/clawBio/tests
python3 -m unittest discover -s Tasks/WindowsAgentArena/tests
uv run --project Agents/utils/web_search python -m unittest discover -s Agents/utils/web_search/tests -v
uv run --project Tasks/SWE-rebench-v2 pytest Tasks/SWE-rebench-v2/tests -q
```

The web search adapter requires Python 3.11 or newer and its own project
dependencies. The SWE-rebench-V2 adapter requires Python 3.12 or newer and
its own project dependencies. Task-selection changes also affect the shared
Harbor suite listed in [Agents/AGENTS.md](../Agents/AGENTS.md#development).
