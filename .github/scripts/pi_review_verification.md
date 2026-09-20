# Pi review discovery and verification

The Pi review action traces changed contracts through callers, configuration,
cleanup paths, and tests. Discovery returns candidate defects with optional
`verification_context` source ranges. Missing tests alone are not defects.
Each candidate is checked by a fresh, tool-free Pi invocation that tries to
refute it using exact base/head source, including unchanged callers selected
during discovery. The verifier reuses the frozen replay citation validator.
It can abstain when evidence is missing; matching citations do not prove the
model's causal reasoning is correct.

## Run modes

`--verification-mode` accepts:

- `off`: existing discovery/publication behavior, without a verifier.
- `shadow`: publish discovery candidates and report verification counts without
  filtering them. This is the workflow default while accuracy is evaluated.
- `enforce`: publish only candidates with a confirmed, citation-validated verdict.
  Rejected candidates are withheld. Missing evidence, invalid citations, budget
  exhaustion, and verifier failures also withhold the candidate and mark review
  coverage partial. They are never counted as a clean review.

For automatic runs, set the repository/environment variable
`LLM_REVIEW_VERIFICATION` to select the mode (default `shadow`). Manual dispatch
has a `verification_mode` choice and defaults `publish_review` to false. This
runs the full discovery and verification path even if that revision already
has a review; it writes only an evaluation report. Set `publish_review` to true
to publish a manual review. The action still executes trusted base code, so a
pre-merge check does not exercise proposed changes until the base includes them.

Local evaluation uses the existing provider environment variables and a trusted
checkout containing the event's base Git objects:

```bash
python3 .github/scripts/pi_pr_review.py \
  --event-path /tmp/pr-event.json \
  --prompt-path .github/scripts/pi_review_prompt.md \
  --verification-mode enforce --no-publish \
  --output /tmp/pi-review-evaluation.json
```

`GITHUB_REPOSITORY`, `GITHUB_WORKSPACE`, `GITHUB_TOKEN`, `LLM_REVIEW_BASE_URL`,
`LLM_REVIEW_MODEL`, and `LLM_REVIEW_API_KEY` retain their existing meanings.
Use secure environment values, never place credentials in event files.
The event must contain `pull_request.number`, `head.sha`, and `base.sha`.
The reviewer checks both revisions again before publishing. Verification mode
is included in the review identity so modes do not share duplicate markers.
`--no-publish` requires `--output`. Output paths must not exist and local reports
are created with mode 0600. CLI callers default to `off` for compatibility.

## Bounds and reports

Verification checks at most six deduplicated candidates, in severity order,
with three concurrent processes and 120 seconds per process (including format
repair). Candidates beyond the limit remain visible as skipped. Source is
fetched once per revision into disposable Git stores; no PR code is executed.
Each candidate receives base/head excerpts around its anchor plus discovery's
caller/guard/test ranges, up to 20 excerpts of 200 lines and 100 KB total.
Renames include both paths. Changed line numbers may differ across revisions;
discovery should request the correct ranges explicitly. Truncated context must
lead to abstention when the omitted code is needed to decide the claim.

Workflow timeout increases from 20 to 30 minutes to accommodate the additional
pass. Shadow and enforce add up to six model invocations, with one bounded JSON
repair each, plus base/head source preparation. Review provider and model
configuration are unchanged. The prompted 16-call discovery budget is still
advisory; this change does not enforce it.

Reports include revisions, discovery prompt/reviewer hashes, model, elapsed
time, coverage, failed lenses, tool counts, original candidate records,
verification verdicts, source metadata, citations, and findings selected for
publication. Status `failed` or `skipped` is distinct from a rejected candidate.
Full source excerpts and raw provider errors are omitted. Credential values are
redacted from free-text fields. Reports still contain source citations and
review text; GitHub artifacts inherit the repository's access controls and are
retained for three days. Stale/duplicate runs record their result without claiming
that a review completed. Review-level failures exit nonzero; partial reviews
remain completed runs with explicit coverage and verification counts.

## Measuring accuracy and cross-file recall

Compare Pi and Codex on the same frozen PR heads and base revisions. Independently
label each distinct root cause as a real introduced bug, false positive, or
unresolved; include unchanged callers and fixed revisions. Keep every revision
of the same PR/root cause in one evaluation partition. Measure precision,
confirmed-bug retention, cross-file bug recall against labeled bugs, abstentions,
failures, and latency. Count a duplicate root cause once. Comment counts alone
are not a quality score, and verifier agreement is not ground truth.

Use no-publish reports and the [frozen replay](pi_review_replay.md) for adjudication.
Only enable enforcement after reviewing false rejections and insufficient-
evidence cases: a bounded tool-free verifier can suppress real cross-file bugs
if discovery did not supply the relevant caller or base ranges. This implementation
has no measured claim of parity with managed Codex review.
