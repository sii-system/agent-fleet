# Agents

Runnable agent and fleet integrations.

For Harbor benchmarks, use the shared runner:

```bash
./scripts/setup.sh
./scripts/run_fleet.sh --taskset terminalbench21 --agent claude-code --workers 1 --detach
```

Store private model settings in the ignored repo-root `config.local.env` or
provide them through the environment. For local datasets, use
`DATASET_NAME=auto DATASET_PATH=/data/tasks bash Agents/utils/common/Harbor/start.sh`.

Agent-specific Harbor code lives in:

- `Harbor-claude-code/`
- `Harbor-opencode/`
- `Harbor-pi/`

Shared Harbor orchestration lives in:

- `utils/common/Harbor/`

OpenClaw fleet code lives in:

- `Openclaw/`

Detailed repository structure is documented in `../STRUCT.md`.
