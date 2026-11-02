# Harbor Claude Code Structure

```text
Agents/Harbor-claude-code/
├── sitecustomize.py
├── apt_sources.py
├── README.md
└── STRUCT.md
```

`sitecustomize.py` contains Claude Code specific runtime hooks used by the Harbor runner.
`apt_sources.py` applies explicitly configured public APT mirrors during
runtime package setup; it also runs on task images with older Python versions.

The realtime Opik hook is loaded from the `third_party/agent-opik-plugin`
submodule:

```text
third_party/agent-opik-plugin/src/sii_opik_plugin/claude_code/claude_realtime_trace.py
```

`Agents/utils/common/Harbor/env.sh` exposes this directory as `HARBOR_CLAUDE_CODE_DIR`.
