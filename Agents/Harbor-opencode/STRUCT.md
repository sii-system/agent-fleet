# Harbor OpenCode Structure

```text
Agents/Harbor-opencode/
├── __init__.py
├── enable_track_harbor.py
├── finalize_opencode_sessions.py
├── opik_opencode_harbor.py
├── README.md
└── STRUCT.md
```

`enable_track_harbor.py` starts OpenCode runs through Harbor with tracing enabled.

`opik_opencode_harbor.py` implements the OpenCode Harbor agent adapter.

`finalize_opencode_sessions.py` collects and finalizes OpenCode trace/session output after a worker finishes.

The OpenCode plugin and realtime hook are loaded from the
`third_party/agent-opik-plugin` submodule:

```text
third_party/agent-opik-plugin/harness/opencode/opik-trace.ts
third_party/agent-opik-plugin/src/sii_opik_plugin/opencode/opencode_realtime_trace.py
```

`Agents/utils/common/Harbor/env.sh` exposes this directory as `HARBOR_OPENCODE_DIR`.
