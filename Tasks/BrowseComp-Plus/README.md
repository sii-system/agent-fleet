# BrowseComp-Plus on Agent Fleet

BrowseComp-Plus is a benchmark taskset, not an Agent. It runs Claude Code,
OpenCode, or Pi through Harbor against one fixed local corpus and a shared MCP
retriever. Query embeddings use a local Qwen model by default and can instead
use a compatible remote API.

## Quick start

Run the normal Agent Fleet setup once. No sibling BrowseComp checkout, Python
activation, Java installation, manual index download, or manual MCP process is
required. The self-contained retriever currently uses Agent Fleet's local
Docker backend; the Agent and judge model APIs may still be remote.

```bash
./scripts/setup.sh

MIN_TEST=1 ./scripts/run_fleet.sh \
  --taskset browsecomp-plus \
  --agent pi \
  --workers 1
```

The first BrowseComp run automatically creates a dedicated Python runtime and
reads only the three required encrypted question columns, then downloads the
fixed corpus and published FAISS index. The default local backend additionally
downloads Qwen3-Embedding-0.6B weights; the remote API backend downloads only
the tokenizer needed to truncate returned snippets. Parquet column projection
skips roughly 2.8 GB of embedded document fields. These one-time assets are reused from
`$AGENT_FLEET_CACHE_DIR/browsecomp-plus`; source code and run logic stay inside
the Agent Fleet checkout.

The default CPU retriever memory-maps the corpus and keeps the embedding model
plus FAISS vectors resident; allow roughly 4 GB of host RAM for the shared MCP
service. A remote embedding backend avoids loading the model weights locally,
but the FAISS index and corpus remain host-side. All concurrent harness workers
reuse that one process. Search returns at most 512 tokenizer tokens per result,
and `get_document` is hard-capped to the first 4096 tokenizer tokens so an
outlier corpus document cannot exhaust an agent's context window.

After the canary succeeds, omit `MIN_TEST` for all queries:

```bash
./scripts/run_fleet.sh -t browsecomp-plus -a pi -n 4
```

`BROWSECOMP_JUDGE_MODE=none` is the default, so a run collects answers and
retrieval statistics without incurring extra judge calls. To score through the
same OpenAI-compatible gateway configured by `setup.sh`:

```bash
BROWSECOMP_JUDGE_MODE=openai MIN_TEST=1 \
  ./scripts/run_fleet.sh -t browsecomp-plus -a pi -n 1
```

That mode reuses `BASE_URL`, `API_KEY`, and `MODEL`. Set
`BROWSECOMP_JUDGE_MODEL=Qwen/Qwen3-32B` (and a dedicated judge endpoint if
needed) for the benchmark's intended judge. A different judge is useful for a
smoke test but its score is not directly comparable to official results.
`BROWSECOMP_JUDGE_MODE=local` installs and invokes the pinned upstream vLLM
judge and therefore requires a visible NVIDIA GPU; CPU hosts should use the
remote `openai` mode.

Optional persistent overrides belong in `config.local.env`; all available
settings are documented in
[`config/browsecomp.env.example`](config/browsecomp.env.example).
If the official Hugging Face endpoint is slow or unavailable on your network,
set the standard `HF_ENDPOINT` variable to a trusted mirror. Connectivity
probing, bootstrap downloads, model loading, and corpus loading all honor it.

## Remote embedding API

The MCP retriever can use any OpenAI-compatible `/v1/embeddings` endpoint
without moving the index or corpus out of Agent Fleet. The API must serve the
same Qwen3-Embedding-0.6B-compatible vector space as the pinned index: model,
query instruction, vector dimension, and normalization must agree. The
retriever sends the exact existing query prefix and rejects dimension-mismatched
responses before FAISS search.

```bash
export BROWSECOMP_EMBEDDING_BACKEND=openai
export BROWSECOMP_EMBEDDING_BASE_URL=https://embedding-gateway.example.com/v1
export BROWSECOMP_EMBEDDING_API_KEY_ENV=EMBEDDING_API_KEY
export BROWSECOMP_EMBEDDING_API_MODEL=Qwen/Qwen3-Embedding-0.6B
export EMBEDDING_API_KEY=your-embedding-key

MIN_TEST=1 ./scripts/run_fleet.sh -t browsecomp-plus -a pi -n 1
```

`BROWSECOMP_EMBEDDING_BASE_URL` may also be a bare API root or a direct
`/v1/embeddings` URL. The key stays only in the host environment and is never
written into the MCP command/state files. Use a dedicated key environment
variable, as in the example, when it must not be shared with normal Agent
Fleet model configuration. By default the host connects directly; set
`BROWSECOMP_EMBEDDING_PROXY_MODE=inherit` only when the endpoint requires an
HTTP(S) proxy.

## Multiple harnesses

Each Harbor run can execute many isolated trials while sharing the read-only
retrieval service:

```bash
./scripts/run_fleet.sh -t browsecomp-plus -a pi -n 4 --run-id bcp-pi
./scripts/run_fleet.sh -t browsecomp-plus -a claude-code -n 4 --run-id bcp-claude
./scripts/run_fleet.sh -t browsecomp-plus -a opencode -n 4 --run-id bcp-opencode
```

FleetSpec launches them concurrently:

```json
[
  {"schema_version": 1, "taskset": "browsecomp-plus", "agent": "pi", "workers": 4},
  {"schema_version": 1, "taskset": "browsecomp-plus", "agent": "claude-code", "workers": 4},
  {"schema_version": 1, "taskset": "browsecomp-plus", "agent": "opencode", "workers": 4}
]
```

```bash
./scripts/run_fleet.sh --spec bcp-matrix.json
```

The bootstrap and MCP launch paths are lock-protected, so concurrent harnesses
reuse the same prepared assets and retriever configuration. Detached runs also
start a small finalizer that waits for Harbor completion and automatically
collects and judges results; its status is written under the run's `runtime/`
directory.

## Ownership and isolation

- `third_party/BrowseComp-Plus` is an unmodified, pinned upstream source.
- `Tasks/BrowseComp-Plus` owns provisioning, task materialization, MCP serving,
  harness adapters, result collection, and remote-judge compatibility.
- Decrypted answers remain host-only under the private cache. Generated Harbor
  tasks contain query text but no answers or qrels.
- Task containers can access only `search` and `get_document`; model and judge
  credentials are not exposed through MCP.
- Outputs live under `runs/<RUN_ID>/browsecomp-plus/`. The automatically managed
  MCP service persists for reuse and requires no user lifecycle commands.

The upstream source and pin are documented in [`UPSTREAM.md`](UPSTREAM.md).

## RL rollout mode

The same materialized tasks can feed the existing rollout worker. Configure
the trusted host-side processor after a `--prepare-only` run:

```bash
export BROWSECOMP_JUDGE_MODE=openai

RUN_ID=bcp-rollout MIN_TEST=1 \
  Tasks/BrowseComp-Plus/scripts/run.sh --prepare-only --agent pi

export RL_BENCHMARK=browsecomp-plus
export RL_RESULT_PROCESSOR="$PWD/Tasks/BrowseComp-Plus/scripts/process_rollout.py"
export RL_DATASET_ROOT="$PWD/runs/bcp-rollout/browsecomp-plus/tasks"
export HARBOR_AGENT_ENV_FILE="$PWD/runs/bcp-rollout/browsecomp-plus/runtime/agent.env"
export HARBOR_MCP_CONFIG="$PWD/runs/bcp-rollout/browsecomp-plus/runtime/mcp.json"
export PI_EXTENSION_SOURCE="$PWD/runs/bcp-rollout/browsecomp-plus/runtime/pi-extensions"

ROLLOUT=1 AGENT=pi RL_AGENT=pi \
  bash Agents/utils/common/Harbor/start.sh --detach
```

The rollout request cannot choose the processor path; the trusted worker
replaces Harbor's execution reward with the scalar judge reward while retaining
both results.
