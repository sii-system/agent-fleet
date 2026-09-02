# Web Research Harbor Adapter

Generates native Harbor tasks for the official BrowseComp and DeepSearchQA
datasets. The two commands share task materialization while keeping source
validation and reward semantics separate.

Official task counts:

| Dataset | Tasks |
| --- | ---: |
| BrowseComp | 1266 |
| DeepSearchQA | 900 |
| Total | 2166 |

Counts were verified with Python's CSV parser against these source hashes:

```text
browse_comp_test_set.csv  7b24471cd5b3eb2a46830a14802b5c029ea62f488ff75a0f88af7923d1454abf
DSQA-full.csv              25d48dcf7efa872e5467032e8b8eedf38d301f59a252d0da95cda584baa78396
```

Generation rejects a source whose parsed task count or SHA-256 does not match
these official files. Validation runs before `--limit` or `--task-ids` filtering.

## Generate

```bash
cd Tasks/WebResearchAdapter
uv run browsecomp-adapter \
  --input /data/browse_comp_test_set.csv \
  --output-dir /data/harbor/browsecomp

uv run deepsearchqa-adapter \
  --input /data/DSQA-full.csv \
  --output-dir /data/harbor/deepsearchqa
```

Use `--limit`, `--task-ids`, `--overwrite`, and `--image` for smaller or
environment-specific generations. `--image` selects both the Dockerfile base
and Harbor's prebuilt `environment.docker_image`; it defaults to
`python:3.12-slim`. OpenSandbox deployments should pass an image already
available to their configured registry.

## Agent Fleet

Register the generated directories with the existing rollout listener:

```bash
export RL_AGENT=claude-code
export RL_ENVIRONMENT_TYPE=opensandbox
export RL_DATASET_ROOTS="browsecomp=/data/harbor/browsecomp,deepsearchqa=/data/harbor/deepsearchqa"
export HARBOR_DISALLOWED_TOOLS="RemoteTrigger AskUserQuestion"
```

Requests select `dataset_name=browsecomp` or `dataset_name=deepsearchqa` and a
generated task ID. Reward and trajectory collection use the existing Harbor
verifier and `rollout_details` path; do not configure `RL_RESULT_PROCESSOR`.

The adapter intentionally does not bundle a search provider or MCP runtime.
Provision search and fetch tools at deployment time, either through native
Claude tools backed by a compatible endpoint or through a deployment-managed
MCP configured for the task or Claude installation. A self-hosted model does
not gain internet access merely because native tool names are visible. When an
external MCP is authoritative, add `WebSearch WebFetch` to
`HARBOR_DISALLOWED_TOOLS` so rollout cannot silently use another backend.

The verifier reuses the current trial's OpenAI-compatible `HARBOR_API_BASE`,
`API_KEY`, and `HARBOR_MODEL`, including trusted request headers. It does not
require a second judge endpoint.

## Test

```bash
uv run python -m unittest discover -s tests -v
```

Validation generated all 2166 tasks and parsed every `task.toml` with Harbor
0.18.0. A Claude Code OpenSandbox smoke also completed the external MCP search,
answer, verifier, and reward path. Search service capacity and judge choice
remain deployment concerns and must be fixed before reporting benchmark scores.
