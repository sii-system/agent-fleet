# Harbor Pi Structure

```text
Agents/Harbor-pi/
├── __init__.py
├── pi_harbor.py
├── README.md
├── STRUCT.md
└── tests/
    └── test_pi_harbor.py
```

`pi_harbor.py` subclasses Harbor's installed Pi agent. It replaces Harbor's
online nvm/npm setup with Agent Fleet's pinned, cache-first Node and Pi runtime;
forwards the custom gateway environment and task instruction over stdin; and
keeps Harbor's native Pi token accounting.

`Agents/utils/common/Harbor/env.sh` exposes this directory as `HARBOR_PI_DIR`,
generates `models.json` and `settings.json` payloads, and selects the adapter
through `pi_harbor:AgentFleetPi`.

`Agents/utils/common/Harbor/prepare_local_deps.sh` owns the shared Node and
preinstalled Pi runtime archives. `harboropik.sh` mounts those artifacts read-only
into task containers and passes only Pi-compatible Harbor agent arguments.
