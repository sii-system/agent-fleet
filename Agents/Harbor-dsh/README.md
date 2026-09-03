# Harbor DeepSeek Harness SDK minimal

`dsh_sdk_minimal_harbor.py` runs DeepSeek Harness's official `sdk-minimal`
profile through the version-matched Python JSON-RPC SDK. The default pins the
DSH CLI release and SDK source to `dsh-v0.1.2-alpha.2` at commit
`0a53fb55bea101816fa226bb964ae2bed71c343b`.

Node, DSH, Python 3.12, and the Python SDK are prepared once on the Agent Fleet
runner, then mounted into each Docker or OpenSandbox task. Agent setup performs
no package-manager downloads. The adapter records the resolved DSH config,
version fingerprints, session JSONL, stdout/stderr, and sampling receipts under
`/logs/agent`.

The adapter supports only `permission_mode=danger-full-access`, the native
DeepSeek provider route, and no Skills or MCP servers. A loopback relay fixes
`reasoning_effort=max`, `temperature=1.0`, and `top_p=0.95` at the model request
boundary. It also preserves the first non-empty tool-call ID and name when an
OpenAI-compatible stream sends empty metadata on later argument deltas.

## Configuration

Keep credentials in ignored `config.local.env` or the process environment.

```bash
export AGENT=dsh-sdk-minimal
export DSH_PROVIDER=deepseek
export DSH_BASE_URL=https://gateway.example.test/v1
export DSH_API_KEY=replace-me
export MODEL=deepseek/your-wire-model-id

export DSH_SDK_MINIMAL_CLI_VERSION=0.1.2-alpha.2
export DSH_SDK_MINIMAL_SOURCE_REF=dsh-v0.1.2-alpha.2
export DSH_SDK_MINIMAL_SOURCE_SHA=0a53fb55bea101816fa226bb964ae2bed71c343b
export DSH_CONTEXT_WINDOW=200000
export DSH_SDK_MINIMAL_MAX_TOKENS=65536
export DSH_PROCESS_RETRY_MAX=0
```

`DSH_SDK_MINIMAL_SOURCE_DIR` may point to an existing DeepSeek Harness checkout
whose `HEAD` exactly matches `DSH_SDK_MINIMAL_SOURCE_SHA`. This supports offline
runtime preparation without weakening source verification.

Run through the shared Harbor entry point:

```bash
export DATASET_NAME=terminalbench21
export HARBOR_ENVIRONMENT_TYPE=docker
export HARBOR_RUNS=1
export HARBOR_N_CONCURRENT=1
bash Agents/utils/common/Harbor/harboropik.sh
```
