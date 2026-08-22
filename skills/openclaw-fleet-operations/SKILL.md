---
name: openclaw-fleet-operations
description: Use when generating, scaling, operating, or debugging the Dockerized OpenClaw gateway fleet in this repository.
---

# OpenClaw Fleet Operations

## Overview

Use this skill for `Agents/Openclaw/`, the Dockerized OpenClaw gateway fleet.
The main path is `Agents/Openclaw/scripts/setup.sh` -> `setup.py` ->
generated `Agents/Openclaw/docker-compose.yml`, `Agents/Openclaw/.env`, and
per-instance `$CONFIG_BASE/<N>/openclaw.json`, followed by Docker Compose and
`openclaw-fleet.sh`.

## Workflow

1. Read `Agents/AGENTS.md`, `Agents/Openclaw/GUIDE.md`, and
   `Agents/Openclaw/SECURITY.md` before changing fleet behavior.
2. Load configuration in the same precedence as `setup.sh`: CLI flags and
   positional `COUNT`, caller environment, `Agents/Openclaw/config/fleet.env`,
   root `config.local.env`, then root `config.env`.
3. Build the image when needed:
   `./Agents/Openclaw/scripts/build-openclaw-image.sh`. Use
   `OPIK_URL` only when Opik tracing should be built into the image.
4. Generate the fleet with `Agents/Openclaw/scripts/setup.sh`. Required model
   gateway values are `BASE_URL` and `API_KEY`; `MODEL` selects the model.
5. Start with
   `docker compose -f Agents/Openclaw/docker-compose.yml up -d`.
6. Operate the fleet with `Agents/Openclaw/scripts/openclaw-fleet.sh` rather
   than editing containers by hand. Selectors are `all`, `3`, `1,3,5`, and
   `2-5`.
7. Use `Agents/Openclaw/scripts/start-session-tui.sh` when the operator needs a
   zellij view of active sessions.

## Hard Rules

- Never hand-edit generated files:
  `Agents/Openclaw/docker-compose.yml`, `Agents/Openclaw/.env`, or
  `$CONFIG_BASE/<N>/openclaw.json`. Regenerate them with
  `Agents/Openclaw/scripts/setup.sh`.
- Preserve generated gateway tokens in `Agents/Openclaw/.env` unless the user
  explicitly wants token rotation.
- Keep real credentials out of committed files. Use `config.local.env` or the
  caller environment.
- Do not relax `tools.exec.security`, `tools.exec.ask`, `WORKSPACE_ONLY`, or
  `DOCKER_COMPOSE_READ_ONLY` without making the benchmark or plugin need
  explicit.
- When changing `CONTAINER_NAME_PREFIX`, `DEFAULT_PORTS_OFFSET`, or `PORT_STEP`,
  verify the management script still targets the intended containers and ports.

## Common Commands

```bash
./Agents/Openclaw/scripts/openclaw-fleet.sh status all
./Agents/Openclaw/scripts/openclaw-fleet.sh probe all
./Agents/Openclaw/scripts/openclaw-fleet.sh logs all --tail 100
./Agents/Openclaw/scripts/openclaw-fleet.sh restart 1,3
./Agents/Openclaw/scripts/openclaw-fleet.sh scale 5
./Agents/Openclaw/scripts/openclaw-fleet.sh plugin-status all
```

## Debugging

- If setup exits early, confirm `BASE_URL` and `API_KEY` are visible after
  config precedence is applied.
- If ports do not match expectations, compute
  `18789 + DEFAULT_PORTS_OFFSET + (N - 1) * PORT_STEP`.
- If a benchmark cannot access files outside the workspace, rerun setup with
  `WORKSPACE_ONLY=false` only for that benchmark path.
- If plugin tracing is missing, verify both the image build and setup used
  `OPIK_URL` plus `OPIK_PROJECT_NAME` were set.
- If generated config paths look wrong, inspect `CONFIG_BASE`,
  `WORKSPACE_BASE`, UID/GID, and host permissions before patching code.

## Output Contract

When reporting fleet work, include:

- instance count, image, `CONFIG_BASE`, `WORKSPACE_BASE`, and port range
- exact setup/build/compose commands used
- whether generated files were regenerated or only inspected
- fleet health from `openclaw-fleet.sh status` or `probe`
- any security-relevant overrides
- tests or smoke checks run

## Validation

Run the affected checks from the repository root:

```bash
python3 -m unittest discover -s Agents/Openclaw/tests
bash Agents/Openclaw/tests/test_build_openclaw_image.sh
bash Agents/Openclaw/tests/test_session_layout.sh
bash Agents/Openclaw/tests/test_start_session_tui.sh
bash Agents/Openclaw/tests/test_stream_openclaw_session_sh.sh
```
