# AGENTS.md — Tasks/

Benchmark task inputs and OpenClaw benchmark runners. Harbor task lists are
consumed by the shared runner in `Agents/utils/common/Harbor/`; PinchBench
and ClawBio run against an OpenClaw fleet from `Agents/Openclaw/` (see
[Agents/AGENTS.md](../Agents/AGENTS.md)). Repo-wide config rules:
[root AGENTS.md](../AGENTS.md).

## Layout

| Path | Role |
| --- | --- |
| `SETA/`, `SWE-smith/`, `SWE-verify/`, `Terminal-bench-2/` | Harbor task lists |
| `DeepSearchQA/` | DeepSearchQA Harbor Hub entrypoint and verifier setup |
| `Pinchbench/` | PinchBench runner for the OpenClaw fleet |
| `clawBio/` | ClawBio bioinformatics benchmark for the OpenClaw fleet |

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

`deepsearchqa` is also a registry alias, resolving to `kgmon/deepsearchqa`.
It has no local task list; see [DeepSearchQA/README.md](DeepSearchQA/README.md)
for its web-tool and judge-endpoint requirements.

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

Run from the repo root:

```bash
python3 -m unittest discover -s Tasks/Pinchbench/tests
python3 -m unittest discover -s Tasks/clawBio/tests
```
