# Harbor Pi

Pi task-container integration for the shared Harbor runner.

Run it through Harbor common:

```bash
AGENT=pi \
DATASET_NAME=terminalbench21 \
MODEL=your-model-id \
BASE_URL=https://your-openai-compatible-endpoint \
API_KEY=sk-fake \
TRACE_TO_OPIK=false \
bash Agents/utils/common/Harbor/start.sh
```

The runner prepares the pinned `@earendil-works/pi-coding-agent` package as a
fully installed runtime archive plus a portable Node runtime once on the host.
Each Harbor task container prefers that pinned Node runtime over any Node binary
included by the task image, extracts the read-only Pi artifact, writes an
isolated provider configuration, and passes the task instruction over stdin.
Pi JSONL events are recorded in `/logs/agent/pi.txt`. The API key is passed only
through `AGENT_FLEET_API_KEY`; generated JSON contains its environment-variable
reference, not the credential.

Defaults are `PI_VERSION=0.81.1` and `PI_THINKING_LEVEL=high`. The model
gateway provider is derived from `BASE_URL` (matching the benchmarked
agent's gateway); `PI_PROVIDER` only overrides it when a distinct name is
needed. Override them in `config.local.env` or the calling environment.

Structure details: [STRUCT.md](./STRUCT.md)
