# AGENTS.md — Agent Fleet

Guidance for coding agents working in this repository. Subsystem runbooks
live in [Agents/AGENTS.md](Agents/AGENTS.md) and
[Tasks/AGENTS.md](Tasks/AGENTS.md).

## What This Repo Is

Runnable agent integrations and benchmark assets for agent evaluation:
Harbor-based benchmark runs (Claude Code, OpenCode, Pi), a remote rollout
service, and a Dockerized OpenClaw gateway fleet with PinchBench and ClawBio.
Shared launchers support direct arguments, FleetSpec JSON, and prompt mode.

## Map

| Directory | Owns | Agent guidance |
| --- | --- | --- |
| `scripts/` | Host setup, fleet launch/FleetSpec, DinD, shared configuration | `scripts/README.md` |
| `Agents/utils/common/Harbor/` | Shared Harbor benchmark runner | `Agents/AGENTS.md` |
| `Agents/utils/rl/` | Remote rollout listener, queue, and workers | `Agents/AGENTS.md` |
| `Agents/Harbor-claude-code/`, `Agents/Harbor-opencode/`, `Agents/Harbor-pi/` | Agent-specific Harbor integration | per-directory `STRUCT.md` |
| `Agents/Openclaw/` | Dockerized OpenClaw gateway fleet | `Agents/AGENTS.md` |
| `Tasks/` | Harbor task inputs/adapters; PinchBench and ClawBio runners | `Tasks/AGENTS.md` |
| `skills/` | Repository operation skills and end-to-end prompts | `skills/README.md` |
| `.github/` | CI workflows, review automation, and validation helpers | `.github/workflows/`, `.github/scripts/tests/` |
| `third_party/agent-opik-plugin/` | Opik tracing plugin (git submodule) | — |

## First-Time Setup

```bash
git submodule update --init --recursive   # required for Opik-enabled runs
./scripts/setup.sh                       # gather config and prepare host tools/runner
```

Setup saves private values in ignored `config.local.env`. To configure
manually, copy `config.env` only if the local file does not already exist.
Prerequisites, managed paths, and DinD setup: [scripts/README.md](scripts/README.md).

Preview a launch with `--dry-run`; use `MIN_TEST=1` for an initial canary:

```bash
./scripts/run_fleet.sh --taskset terminalbench21 --agent claude-code --workers 1 --dry-run
MIN_TEST=1 ./scripts/run_fleet.sh --taskset terminalbench21 --agent claude-code --workers 1
```

## Configuration Rules

- `config.env` is the committed, public-safe template. `config.local.env`
  (git-ignored) is sourced after it and overrides it. Runtime environment
  variables override both.
- Reuse `scripts/config_loader.sh` for shell entry points. Preserve explicit
  empty caller values as well as nonempty overrides.
- `OPIK_URL` is the only tracing switch: an endpoint enables upload; empty
  disables it. `OPIK_API_KEY` is optional for authless endpoints.
  Do not reintroduce `TRACE_TO_OPIK`, `OPIK_PLUGIN`, or `OPIK_MODE` gates.
- OpenClaw-family runners (fleet setup, PinchBench, ClawBio) also read
  `Agents/Openclaw/config/fleet.env` for fleet-wide values such as `COUNT`.
- Secrets (`API_KEY`, `OPIK_API_KEY`, gateway tokens) go only in
  `config.local.env` or the shell environment — never in committed files.
  Use obviously fake placeholders in docs and tests.
  Provider credential files such as `.s3-profiles/` also remain git-ignored;
  see the backend runbooks before changing their handling.

## Hard Rules

- Never hand-edit generated files; regenerate them with
  `Agents/Openclaw/scripts/setup.sh`:
  - `Agents/Openclaw/docker-compose.yml`
  - `Agents/Openclaw/.env` (generated gateway tokens)
  - `$CONFIG_BASE/<N>/openclaw.json` (default `~/openclaw-instances/<N>/`)
- Every shell script uses `set -euo pipefail`; keep that in new scripts.
- Shell entry points own environment and process orchestration. Put structured
  parsing, file mutation, and delegated data workflows in focused Python
  helpers, following the boundaries in `STRUCT.md`.
- Shared benchmark settings use `HARBOR_*`; rollout-specific settings use
  `RL_*`. Keep task inputs under `Tasks/` and agent integrations under `Agents/`.

## Development Checks

Run affected suites before committing. From the repo root, repository/setup/CI
tests are:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 -m unittest discover -s scripts/tests -v
PYTHONPATH=. python3 -m unittest discover -s .github/scripts/tests -v
bash scripts/tests/test_prerequisites.sh
bash scripts/tests/test_dind_cgroup_v2.sh
bash scripts/tests/test_dind_run.sh
```

Agent and task suites are listed in the nested AGENTS.md files. The portable
CI baseline, including Python and test dependencies, is
[.github/workflows/pr-validation.yml](.github/workflows/pr-validation.yml).
Validate changed shell files with `bash -n`. For Python changes, run
`ruff check --config .github/ruff.toml` using the version required by that
config. Setup installs an advisory Git hook; CI also runs Ruff.

## Pull Requests

When opening a PR, fill the sections from
[.github/pull_request_template.md](.github/pull_request_template.md): Why the
change, Summary of the change, and Other details.

## More Docs

- Repository layout: [STRUCT.md](STRUCT.md)
- Harbor runner internals: [Agents/utils/common/Harbor/STRUCT.md](Agents/utils/common/Harbor/STRUCT.md)
- OpenClaw fleet guide and security policy: [Agents/Openclaw/GUIDE.md](Agents/Openclaw/GUIDE.md), [Agents/Openclaw/SECURITY.md](Agents/Openclaw/SECURITY.md)
