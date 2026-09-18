# SWE-rebench-V2 Harbor Adapter

This directory contains Agent Fleet's canonical converter for the official
SWE-rebench-V2 dataset. Repository checkout and `install_config.install` run
while the task image is built; `instruction.md` contains only the requested
software change. The dataset's top-level prebuilt `image_name` is retained as
verifier metadata but is never a runtime dependency.

The previous TaskTrove-based integration remains available as a
[third-party alternative](../SWE-rebench-v2-TaskTrove/README.md). It is not the
canonical source for this adapter because it contains only a subset of the
official records and places repository setup in agent instructions.

## Attribution

The adapter code is adapted from the official
[SWE-rebench-V2 builder](https://github.com/SWE-rebench/SWE-rebench-V2),
pinned at commit `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`. In particular,
the environment template, test execution and evaluation semantics, log
parsers, and test status constants follow the corresponding upstream builder
files. The input records come from the
[Nebius SWE-rebench-V2 dataset](https://huggingface.co/datasets/nebius/SWE-rebench-V2).

## Pinned inputs

The adapter was developed and validated against:

- SWE-rebench-V2 builder commit
  `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`
- Official dataset snapshot with 32,079 rows
- Parquet SHA-256
  `0e0bf9355f892ad74ae98d4e1c404f39fd6654a8e351ee3e6ab162e4a64cd3ad`
- Harbor 0.18.0 task contract

The converter revision is the Agent Fleet commit used to generate the task
directories. Record that commit together with the dataset snapshot and the
base-image mapping for every materialization.

## Prepare dependencies

Keep the virtual environment and uv cache outside the source checkout.
Python dependencies resolve through an external package index; on YiCloud the
domestic PyPI mirror works without extra configuration:

```bash
export UV_PROJECT_ENVIRONMENT=/data/harbor-envs/swe-rebench-v2-adapter
export UV_CACHE_DIR=/data/harbor-caches/uv
export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
uv sync --project Tasks/SWE-rebench-v2
```

## Generate tasks

Generate one task first:

```bash
uv run --project Tasks/SWE-rebench-v2 swe-rebench-v2 \
  --dataset-path <official-dataset-directory> \
  --instance-id unidata__netcdf-c-1692 \
  --output-dir <generated-harbor-dataset> \
  --dataset-source nebius/SWE-rebench-V2@<dataset-revision>
```

Use `--task-ids` for an explicit sample. Full conversion requires `--all`:

```bash
uv run --project Tasks/SWE-rebench-v2 swe-rebench-v2 \
  --dataset-path <official-dataset-directory> \
  --all \
  --output-dir <generated-harbor-dataset> \
  --summary-json <conversion-summary.json> \
  --dataset-source nebius/SWE-rebench-V2@<dataset-revision>
```

The summary reports converted and failed tasks plus resolved base-image names.
Generated task directories are runtime data and must not be committed here.
Generated Dockerfiles retain logical builder image names. Configure
`HARBOR_OPENSANDBOX_BASE_IMAGE_REGISTRY` when prebuilding or running
through OpenSandbox so the image manager resolves those names through the
configured base Registry without embedding an environment-specific Registry
in the dataset.

The published dataset contains records with an empty
`install_config.install`; those records preserve the official empty-loop
behavior. Records with a null `problem_statement` use the non-answer-bearing
`pr_description` as their instruction.

## Prebuild and run

Prebuild through the existing OpenSandbox image pipeline:

```bash
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  <generated-harbor-dataset> agent-fleet-swe-rebench-v2
```

Run the local dataset with the benchmark alias. The alias selects the portable
Python verifier bundle required by base images that do not provide Python:

```bash
DATASET_PATH=<generated-harbor-dataset> \
HARBOR_ENVIRONMENT_TYPE=opensandbox \
./scripts/run_fleet.sh \
  --taskset agent-fleet-swe-rebench-v2 \
  --agent claude-code \
  --workers 1
```

The harness provides this verifier-only runtime on Docker, E2B, qz, and
OpenSandbox without changing the generated environment image. Docker mounts
the bundle read-only; managed Sandbox backends upload it after Sandbox
creation. Updating the verifier runtime therefore does not require rebuilding
or republishing task images.

Use `--task <instance-id>` for an exact local task selection. Remote rollout
deployments can register the same directory with:

```bash
export RL_DATASET_ROOTS="agent-fleet-swe-rebench-v2=<generated-harbor-dataset>"
```

## Generated layout and security boundary

Each generated task contains `task.toml`, `instruction.md`, an environment
Dockerfile, verifier files under `tests/`, and an optional Oracle solution.
`tests/config.json` intentionally preserves answer-bearing fields such as the
gold patch and expected tests. Harbor mounts verifier files after the agent
run; these fields must never be copied into the agent-visible image.

The bundled log parsers come from the pinned SWE-rebench-V2 builder. The
wrapper selects the record's exact parser and compares the parsed PASSED set
with `FAIL_TO_PASS + PASS_TO_PASS`, matching the builder evaluator.

## Known limitations

SWE-rebench-V2 contains upstream environment and verifier defects. They are
audited task by task and are not silently rewritten as part of general
conversion. The adapter contains only narrow, documented build compatibility
mappings for cases whose original environment cannot otherwise be
materialized. Benchmark-level fixes require separate evidence and validation.

## Tests

```bash
uv run --project Tasks/SWE-rebench-v2 pytest \
  Tasks/SWE-rebench-v2/tests -q
```
