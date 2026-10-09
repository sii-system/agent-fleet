# Self-hosted Harbor nightly operations

`harbor-self-hosted-nightly.yml` samples 20 tasks from one benchmark every
four hours. `harbor-e2e-validation.yml` runs all Terminal-Bench 2.1 tasks on its
schedule; a manual dispatch defaults to its four-task canary set.

## Model availability

Both workflows use the `self-hosted-env` environment:

- `LLM_REVIEW_BASE_URL`: gateway API endpoint.
- `LLM_REVIEW_API_KEY`: gateway credential (secret).
- `HARBOR_NIGHTLY_MODEL`: optional nightly-specific model name. When unset,
  the workflows fall back to `LLM_REVIEW_MODEL`.

Prefer a maintained gateway alias over a short-lived deployment ID when the
provider offers one for the intended model. Confirm that the credential can
use that alias before changing it. The nightly-specific variable lets the
nightlies use a different deployment without changing PR-review routing.

Before setup or benchmark execution, both workflows send a small completion
request to the configured model. They check chat completions for summaries
and additionally the Messages route for Claude Code. Authentication, missing
model, and invalid response failures stop the job immediately; connection
failures, HTTP 408/429, and server failures have at most three attempts per
route. The step has a three-minute ceiling. Response bodies and credentials
are not logged.

A model that disappears mid-run can still fail trials. The preflight checks
availability at startup; it does not change models or weaken the health gate.

The random catalog currently excludes GAIA because the nightly model supports
text input only. On 2026-10-07, GAIA image reads received HTTP 400 with
`Model only supports text input`; text-only preflight requests still passed.
Re-add GAIA only after configuring a model that accepts image content through
the agent's API route and verifying a representative image task. That run
also had three agent timeouts; removing the image incompatibility alone does
not establish that all tasks will complete within their time budgets.

## Pinned runtime preparation

Both workflows call `Agents/utils/common/Harbor/setup_runner_env.sh` during
prerequisite validation and export their own runner directories under
`RUNNER_TEMP`. Setup refreshes stale package pins before workload startup,
which only validates the environment. The TB nightly previously reused a
host runner with `yicloud-sdk-python==0.3.1` after the repository pin advanced
to `0.4.1`, preventing every trial from starting.

Some task images, including RuneBench, already contain a native Claude
executable at `~/.local/bin/claude`. The Claude installer moves that entry
into a temporary backup before installing Harbor's pinned npm package.
Without this step npm returned `EEXIST`, causing all 20 sampled RuneBench
tasks on 2026-10-07 to fail during agent setup.

## Docker registry failures

A reachable registry endpoint does not prove that all task images are
available. Inspect the failing trial's `exception_info` and Docker build
output. For example, on 2026-09-15 the configured mirror returned HTTP 403
and `docker.io/swerebench/* is forbidden`, preventing 19 of 20 sampled
SWE-rebench task environments from building.

For a namespace denial:

1. Confirm the selected task's base image and failing registry hostname.
2. Arrange an allowed registry route or pre-populate those exact images in
   the runner's Docker cache from an authorized source.
3. Verify a representative task environment builds on each runner before
   launching the full suite again.

Repeating the same forbidden pull or using `HARBOR_FORCE_BUILD=1` cannot fix a
Dockerfile whose `FROM` image is itself denied. Changing the shared Docker
daemon affects other runners on the host; coordinate any daemon restart.
Keep the sampled task list and failure report so unavailable dependencies
remain visible.

## Checkout and artifact recovery

Manual random-nightly dispatches can set `benchmark` to a catalog entry and
`tasks` to 1–20 exact task names to replay a failing sample. Empty task input
samples 20 tasks from the selected benchmark; empty benchmark input retains
random selection. Explicit tasks require an explicit benchmark. Selection
validates task membership before launching Harbor.

The optional `revision` input selects the checkout, including upstream PR refs
such as `refs/pull/227/head`, while the workflow uses upstream's
`self-hosted-env` credentials. Dispatch only trusted revisions. Empty revision
uses the workflow commit. Scheduled runs retain their normal checkout and
random selection. The workflow preserves its task selector before checking out
the requested revision so older revisions can use the replay inputs too.

Both workflows repair run artifact ownership before checkout and after
cleanup, before artifact staging. Runner users need noninteractive sudo
access to `chown` for root-owned Docker artifacts. Recovery operates on the
run directory without traversing symlinks or expanding group/world access.

Rerunning an old workflow run uses its original workflow revision. To test a
merged workflow fix, dispatch a new run on the updated branch. Use the E2E
manual canary first, then its full scheduled suite.
