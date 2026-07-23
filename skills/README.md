# Skills

This directory contains Codex/Claude skill packages for operating and
validating this repository. Each skill lives in its own directory and is
described by a `SKILL.md` file with front matter:

```markdown
---
name: skill-name
description: Use when ...
---
```

## Available Skills

| Skill | Use when |
| --- | --- |
| `openclaw-fleet-operations` | Generating, scaling, operating, or debugging the Dockerized OpenClaw gateway fleet. |
| `openclaw-benchmark-runners` | Running PinchBench or ClawBio benchmarks against an OpenClaw gateway fleet. |
| `harbor-benchmark-runner` | Configuring, launching, monitoring, or debugging Harbor benchmark runs for Claude Code or OpenCode. |

## Usage Examples

Ask the agent to use a skill by name when you want that workflow's rules and
validation checks applied:

```text
Use openclaw-fleet-operations to create a 10-instance OpenClaw fleet with
MODEL=glm-5.1-fp8 and verify all gateways are healthy.
```

```text
Use openclaw-benchmark-runners to run 3 PinchBench sanity tasks against the
current OpenClaw fleet and summarize pass/fail counts.
```

```text
Use harbor-benchmark-runner to run 3 SETA Harbor tasks with AGENT=claude-code,
TOTAL_WORKERS=3, TB_N_CONCURRENT=3, and inspect online-analysis results.
```

You can also combine skills for end-to-end validation:

```text
Use openclaw-fleet-operations and openclaw-benchmark-runners to regenerate a
5-instance fleet, run ClawBio once, and report any operator failures.
```

## Install Skills

Run these commands from the repository root. Symlinks keep the installed skills
updated as the checkout changes; use `cp -a` instead of `ln -sfn` when the
runtime needs a standalone copy.

### Codex

Codex loads personal skills from `${CODEX_HOME:-$HOME/.codex}/skills`:

```bash
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$CODEX_SKILLS_DIR"

for skill in \
  harbor-benchmark-runner \
  openclaw-fleet-operations \
  openclaw-benchmark-runners
do
  ln -sfn "$PWD/skills/$skill" "$CODEX_SKILLS_DIR/$skill"
done
```

### Claude Code

Claude Code can load these skills through a small plugin wrapper:

```bash
CLAUDE_PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$HOME/.claude/skills/agent-fleet}"
mkdir -p "$CLAUDE_PLUGIN_DIR/.claude-plugin"

for skill in \
  harbor-benchmark-runner \
  openclaw-fleet-operations \
  openclaw-benchmark-runners
do
  ln -sfn "$PWD/skills/$skill" "$CLAUDE_PLUGIN_DIR/$skill"
done

cat > "$CLAUDE_PLUGIN_DIR/.claude-plugin/plugin.json" <<'JSON'
{
  "$schema": "https://anthropic.com/claude-code/plugin.schema.json",
  "name": "agent-fleet",
  "version": "0.1.0",
  "description": "Agent Fleet operation skills for Harbor, OpenClaw, and benchmarks.",
  "skills": [
    "./harbor-benchmark-runner",
    "./openclaw-fleet-operations",
    "./openclaw-benchmark-runners"
  ]
}
JSON
```

Load the plugin with Claude Code using the plugin directory:

```bash
claude --plugin-dir "$CLAUDE_PLUGIN_DIR"
```

### OpenCode Or Pi

For OpenCode, Pi, or another `SKILL.md`-compatible runtime, install the same
skill directories into that runtime's configured skill directory:

```bash
RUNTIME_SKILLS_DIR="/path/to/runtime/skills"
mkdir -p "$RUNTIME_SKILLS_DIR"

for skill in \
  harbor-benchmark-runner \
  openclaw-fleet-operations \
  openclaw-benchmark-runners
do
  ln -sfn "$PWD/skills/$skill" "$RUNTIME_SKILLS_DIR/$skill"
done
```

Keep model gateway credentials out of this README and committed files. Put
private values such as `BASE_URL`, `MODEL`, and `API_KEY` in `config.local.env`
or pass them in the caller environment.

## Layout

```text
skills/
  <skill-name>/
    SKILL.md
    agents/
```

`SKILL.md` is the source of truth for the workflow, rules, debugging notes, and
validation commands. The optional `agents/` directory can hold agent-specific
instructions or supporting files used by that skill.

## Maintenance Guidelines

- Keep skill names stable; users and automation may reference them directly.
- Keep descriptions action-oriented so the right skill is selected for the
  task.
- Put public-safe defaults in repository files. Keep real credentials and
  private endpoint values in `config.local.env` or the caller environment.
- Update the relevant validation commands when changing scripts, workflows, or
  benchmark entrypoints.
- Add new skills as a directory containing `SKILL.md`, then update the table in
  this README.
