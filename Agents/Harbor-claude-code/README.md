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
OPIK_URL= \
bash start.sh
```

This directory is not usually launched directly. Use `Agents/utils/common/Harbor/start.sh`.
For traced runs, the realtime hook is loaded from the
`third_party/agent-opik-plugin` submodule; trace-off runs do not require it.

Structure details: [STRUCT.md](./STRUCT.md)

If task containers cannot reach the default Debian/Ubuntu package servers, set
`HARBOR_CC_APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn` in `config.local.env`
or the runner environment. Installation rewrites standard Debian/Ubuntu sources
in both `.list` and Deb822 `.sources` files; third-party repositories stay intact.
An empty value preserves the image's sources. The scheduled workflows read this
setting from the `self-hosted-env` GitHub environment variable of the same name.
