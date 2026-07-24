# Pi PR Review Hardening Design

## Goal

Make PR #23's pi-based reviewer able to inspect the trusted base checkout
without allowing untrusted pull request content to read arbitrary files from
the hosted or self-hosted runner.

## Scope

The refinement is limited to the new pi reviewer, its workflow contracts, and
its tests. The existing direct-LLM reviewer remains available for rollback.
The Harbor analyzer path gate is reused rather than duplicating another
filesystem policy implementation.

## Runtime Design

`PiClient` receives the checked-out repository root and starts pi with that
directory as its working directory. The workflow entrypoint takes the root
from `GITHUB_WORKSPACE`, which points at the trusted `base.sha` checkout used
by both `pull_request_target` workflows.

Pi's built-in tools are disabled. The existing Harbor analyzer path-gate
extension registers replacement `read`, `grep`, `find`, and `ls` tools, and
its allowlist contains only the repository root. The extension resolves real
paths before access, so absolute paths, parent traversal, and symlinks cannot
escape the checkout. Tool discovery, skills, prompt templates, themes,
context files, and implicit project approval remain disabled.

The subprocess environment remains minimal. It contains the model API key,
network and certificate settings, the isolated pi runtime directory, and the
single repository allowlist needed by the path gate. The GitHub token is not
passed to pi.

## Endpoint Handling

`LLM_REVIEW_BASE_URL` keeps its existing contract as a complete
Chat Completions endpoint. The pi adapter validates its scheme and host and
removes only a final `/chat/completions` path suffix. It does not append
`/v1` or otherwise rewrite custom gateway path prefixes. Query parameters and
fragments are unsupported; operators must remove them or move their values to
the appropriate request headers before configuring the endpoint.

## Response Handling

The reviewer continues to require exactly one JSON object. It accepts either
bare JSON or a single Markdown JSON fence, including a compact fence without
line breaks. Trailing prose and non-object JSON remain errors.

## Failure Behavior

Missing or invalid repository roots and missing path-gate extensions fail
before pi starts. Pi startup, timeout, provider, lifecycle, and invalid-output
failures remain explicit workflow failures. An empty successful assistant
message remains partial coverage rather than silently claiming a complete
review.

## Verification

Regression tests are written before implementation and must demonstrate:

- pi runs with the repository root as its working directory;
- built-in tools are disabled and only path-gated read-only tools are enabled;
- the path-gate extension and repository allowlist reach the subprocess;
- custom endpoint prefixes are preserved without an injected `/v1`;
- compact fenced JSON is accepted while trailing content is rejected;
- both workflow contracts still use trusted-base checkout and separate review
  identities.

After unit tests, a local smoke test with pi 0.81.1 will verify that the
explicit extension loads and that repository reads succeed while an
out-of-root read is denied. The full affected test suite and workflow syntax
checks run before the branch is published.
