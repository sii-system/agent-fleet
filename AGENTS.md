# AGENTS.md — Agent Fleet

Guidance for coding agents working in this repository. Subsystem runbooks
live in [Agents/AGENTS.md](Agents/AGENTS.md) and
[Tasks/AGENTS.md](Tasks/AGENTS.md).

## What This Repo Is

Runnable agent integrations and benchmark assets for agent evaluation:
Harbor-based benchmark runs (Claude Code, OpenCode), a Dockerized OpenClaw
gateway fleet, and its benchmarks (PinchBench, ClawBio).

## Map

| Directory | Owns | Agent guidance |
| --- | --- | --- |
| `Agents/utils/common/Harbor/` | Shared Harbor benchmark runner | `Agents/AGENTS.md` |
| `Agents/Harbor-claude-code/`, `Agents/Harbor-opencode/` | Agent-specific Harbor integration | per-directory `STRUCT.md` |
| `Agents/Openclaw/` | Dockerized OpenClaw gateway fleet | `Agents/AGENTS.md` |
| `Tasks/` | Harbor task lists; PinchBench and ClawBio runners | `Tasks/AGENTS.md` |
| `third_party/agent-opik-plugin/` | Opik tracing plugin (git submodule, tag `v0.1.0`) | — |

## First-Time Setup

```bash
git submodule update --init --recursive   # required for Opik-enabled runs
cp config.env config.local.env            # put real values in the local copy
```

## Configuration Rules

- `config.env` is the committed, public-safe template. `config.local.env`
  (git-ignored) is sourced after it and overrides it. Runtime environment
  variables override both.
- OpenClaw-family runners (fleet setup, PinchBench, ClawBio) also read
  `Agents/Openclaw/config/fleet.env` for fleet-wide values such as `COUNT`.
- Secrets (`API_KEY`, `OPIK_API_KEY`, gateway tokens) go only in
  `config.local.env` or the shell environment — never in committed files.
  Use obviously fake placeholders in docs and tests.

## Hard Rules

- Never hand-edit generated files; regenerate them with
  `Agents/Openclaw/scripts/setup.sh`:
  - `Agents/Openclaw/docker-compose.yml`
  - `Agents/Openclaw/.env` (generated gateway tokens)
  - `$CONFIG_BASE/<N>/openclaw.json` (default `~/openclaw-instances/<N>/`)
- Every shell script uses `set -euo pipefail`; keep that in new scripts.
- Tests live next to each subsystem (`Agents/Openclaw/tests/`,
  `Tasks/Pinchbench/tests/`, `Tasks/clawBio/tests/`). Run the affected suite
  before committing — commands are in the nested AGENTS.md files.

## Pull Requests

When opening a PR, fill the sections from
[.github/pull_request_template.md](.github/pull_request_template.md): Why the
change, Summary of the change, and Other details.

## More Docs

- Repository layout: [STRUCT.md](STRUCT.md)
- Harbor runner internals: [Agents/utils/common/Harbor/STRUCT.md](Agents/utils/common/Harbor/STRUCT.md)
- OpenClaw fleet guide and security policy: [Agents/Openclaw/GUIDE.md](Agents/Openclaw/GUIDE.md), [Agents/Openclaw/SECURITY.md](Agents/Openclaw/SECURITY.md)
