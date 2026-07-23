# Harbor Claude Code

Claude Code integration used by the shared Harbor runner.

Run it through Harbor common:

```bash
cd Agents/utils/common/Harbor

AGENT=claude-code \
DATASET_NAME=seta \
MODEL=minimax2.7 \
BASE_URL="https://your-openai-compatible-endpoint" \
API_KEY="sk-xxx" \
TRACE_TO_OPIK=false \
bash start.sh
```

This directory is not usually launched directly. Use `Agents/utils/common/Harbor/start.sh`.
For traced runs, the realtime hook is loaded from the
`third_party/agent-opik-plugin` submodule; trace-off runs do not require it.

Structure details: [STRUCT.md](./STRUCT.md)
