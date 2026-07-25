# Pi PR Review Hardening Design

## Goal

Make PR #23's pi-based reviewer able to inspect the trusted base checkout
without allowing untrusted pull request content to read arbitrary files from
the hosted or self-hosted runner.

## Scope

The refinement primarily changes the new pi reviewer, its workflow contracts,
and its tests. The existing direct-LLM reviewer remains available for rollback.
The Harbor analyzer path gate is reused rather than duplicating another
filesystem policy implementation. That shared extension also gains bounded
tool outputs, grep/glob inputs, and a non-regular-expression wildcard matcher,
so Harbor analyzer users receive the same result caps, input limits, and
`*`/`?` glob semantics.

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

The PR reviewer sets a reviewer-only path-gate policy that treats every grep
pattern as literal text even if the model requests regular-expression matching.
Harbor analyzer callers that do not set this policy retain their existing regex
grep behavior. Grep patterns are limited to 1024 characters, while grep and
find glob patterns are limited to 256 characters. Glob matching still treats
only `*` and `?` as wildcards, but no longer compiles model-supplied globs as
JavaScript regular expressions.

Each `read`, `grep`, `find`, and `ls` result is limited to 50 KiB of UTF-8
output. Reads remain limited to 1,200 lines, while `find` and `ls` requests are
clamped to 200 results. The PR reviewer additionally sets a 16-call limit for
each diff chunk. The path gate aborts pi before an over-budget call executes,
and the Python stream validator rejects any otherwise successful stream that
reports more than 16 tool executions. Shared Harbor analyzers do not set the
call-limit environment variable and therefore retain an unlimited number of
calls, but their individual tool results receive the shared output caps.

The subprocess environment remains minimal. It contains the model API key,
network and certificate settings, the isolated pi runtime directory, and the
single repository allowlist needed by the path gate. The GitHub token is not
passed to pi.

## Endpoint Handling

`LLM_REVIEW_BASE_URL` keeps its existing contract as a complete
Chat Completions endpoint. The pi adapter validates its scheme and host and
removes only a final `/chat/completions` path suffix. It does not append
`/v1` or otherwise rewrite custom gateway path prefixes. Query parameters and
fragments are unsupported; operators must remove them or configure a compatible
endpoint that does not require them.

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
- reviewer grep is forced to literal matching while Harbor regex grep remains
  available when the reviewer policy is absent;
- each PR-review chunk is limited to 16 tool calls while Harbor remains
  unlimited when the reviewer environment variable is absent;
- read, grep, find, and ls results remain within 50 KiB, with requested find
  and ls limits clamped to 200;
- grep and glob inputs are bounded and glob matching does not compile a regular
  expression;
- custom endpoint prefixes are preserved without an injected `/v1`;
- compact fenced JSON is accepted while trailing content is rejected;
- both workflow contracts still use trusted-base checkout and separate review
  identities.

After unit tests, a local smoke test with pi 0.81.1 will verify that the
explicit extension loads and that repository reads succeed while an
out-of-root read is denied. The full affected test suite and workflow syntax
checks run before the branch is published.
