# Contributing

Guidelines for contributors and coding agents. These conventions come from
review history in this repo; follow them to get a PR merged with minimal
back-and-forth. Agent-specific runbooks live in [AGENTS.md](AGENTS.md) and the
nested `*/AGENTS.md` files.

## Pull Requests

- Fill every section of the
  [PR template](.github/pull_request_template.md): Why the change, Summary of
  the change, Other details.
- **Squash and merge.** Edit the squash *Extended description* — reuse the key
  points from the PR body rather than leaving the raw commit list.
- **Keep the PR body accurate.** When review feedback drops something, update
  the body to match; don't leave stale claims.
- **One committer identity per PR.** Verify `git config user.name` /
  `user.email` before committing so a PR doesn't mix accounts. Fix authorship on
  the branch if it drifts.
- **Sequence stacked PRs.** Don't reference a file or script that only exists in
  an unmerged PR; land the dependency first.
- Rebase onto the latest upstream `main` before pushing; no merge commits.

## Testing & Verification

Every substantive PR includes a **Test plan / Verification** section listing the
commands you actually ran. The expected bar:

- `bash -n <script>` syntax-checks every shell script you touched.
- Run the affected `unittest` suite(s) — tests live next to each subsystem
  (`Agents/utils/common/Harbor/tests/`, `Agents/Openclaw/tests/`,
  `Tasks/Pinchbench/tests/`, `Tasks/clawBio/tests/`); commands are in the nested
  `AGENTS.md` files.
- Verify any paths/links you reference actually exist.
- Scan your changed files for leaked secrets before pushing (see Security
  below).

## Configuration & Environment Variables

- **One canonical name per setting** — no aliases. Standardize on the repo var
  (`API_KEY`, `MODEL`, `GOPROXY`, `PIP_INDEX_URL`, `TB_CC_*`, …); don't also
  accept a second spelling for the same value.
- **Precedence:** caller env > `config.local.env` > `config.env` > built-in
  defaults. OpenClaw-family flows (fleet setup, PinchBench, ClawBio) insert
  `Agents/Openclaw/config/fleet.env` above the repo config files, so their order
  is caller env > `fleet.env` > `config.local.env` > `config.env` > defaults.
  Snapshot caller env and re-apply it after sourcing config files so one-off
  overrides win.
- **Never overwrite user files.** Merge managed keys into `~/.bashrc`,
  `~/.claude/settings.json`, and `config.local.env`; back up first, and *warn*
  (don't silently reset) when an existing file fails to parse.
- **`config.local.env` uses plain `KEY=value`** — no `shlex.quote`. The repo's
  readers parse with `partition("=")` and do not shell-unquote.
- **Normalize `BASE_URL` idempotently.** It may or may not end in `/v1`; don't
  produce `.../v1/v1`.
- **Consolidate ignores** into the repo-root `.gitignore` instead of adding
  per-directory files that duplicate existing patterns.

## Security

- **No internal infrastructure values** in committed files — no internal
  hostnames, private registries/mirrors, real IPs, tokens, or team-internal
  identifiers. Use RFC 5737 documentation IPs, `host-1`-style placeholders, and
  move real host lists into a git-ignored config generated from a
  `.example.json`.
- **Secrets go only in `config.local.env` or the shell environment** — never in
  committed files. Use obviously fake placeholders in docs and tests.
- **Collect secrets with a silent read** (`read -rsp ...; echo`) so tokens don't
  land in terminal scrollback.
- **Shell-escape interpolated values** when generating shell code / `.bashrc`
  (guard against `$`, backticks, quotes, backslashes).

## Portability

- **Scripts work from any clone or worktree** — derive paths from `SCRIPT_DIR`;
  don't hardcode `$HOME/agent-fleet`. Keep an explicit override where useful.
- **Portable shell** — prefer a Python helper over fragile in-place edits such
  as `sed -i`.
- **Every shell script starts with `set -euo pipefail`.**
- **Never hand-edit generated files** (see [AGENTS.md](AGENTS.md) Hard Rules);
  regenerate them with the documented script.

## Documentation Style

- **Don't over-explain.** List verified versions and the install command; skip
  the rationale for why a tool is used.
- **Give concrete examples** (e.g. a config snippet for a locally-deployed
  endpoint).
- **Prerequisites are an explicit "Step 0,"** not a collapsed aside. Explain
  what each env var means at the step where it's introduced.
- **Separate usage from design/implementation.**
- **Use precise terminology** — e.g. "launching fleet to run tasks," not
  "running benchmark."
- **English only** — no CJK characters in committed docs.
- **Don't state unverified claims** (e.g. a "known bug"); verify against the code
  first, or leave it out.
