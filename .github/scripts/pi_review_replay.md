# Pi finding verification replay

`pi_review_replay.py` evaluates one frozen review candidate against explicitly
selected base/head source excerpts. It writes a local JSON artifact and never
calls GitHub publication APIs. The live review action also reuses its source
reader and citation validator through the separate
[discovery and verification pipeline](pi_review_verification.md). This frozen
replay remains independent of live publication and automatic context selection.

## Run a case

Use a trusted checkout containing this script and a Pi installation. Supply
`LLM_REVIEW_BASE_URL`, `LLM_REVIEW_MODEL`, and `LLM_REVIEW_API_KEY` through the
existing secure environment. `GITHUB_TOKEN` is optional for public source and
should have read-only repository access when needed. Do not place credentials
in cases or artifacts.

```bash
python3 .github/scripts/pi_review_replay.py \
  --case .github/scripts/tests/fixtures/pi-review-replay/pr178-modules-already-sourced.json \
  --repository-root "$PWD" \
  --output /tmp/pr178-verification.json
```

The output path must not exist. Artifacts are created with mode 0600. Each case
starts a fresh tool-free Pi process with extension, skill, and context-file
discovery disabled, in an empty temporary directory. Base/head source is read
by Python from disposable Git stores using the bounded fetch from the reviewer;
PR code is never checked out, imported, or executed. Excerpts and findings are
sent to the configured model provider. No API calls retrieve current PR state:
replays use the frozen SHAs and candidate in the case.

## Case and evidence contract

The version-1 JSON case contains `id`, canonical `repository`, full `base_sha`
and `head_sha`, one `finding` using the reviewer's existing fields, and a
`context` array. Each context entry specifies `revision` (`base` or `head`),
`path`, `start_line`, and `end_line`. Up to 20 excerpts of at most 200 lines each
are allowed. UTF-8 regular files up to 2 MiB are supported; unsupported or larger
files are explicitly omitted. Missing paths are explicitly absent. The encoded
model input must fit 100,000 bytes; narrow the ranges if preparation fails.

Choose callers, guards, tests, and changed implementations needed to adjudicate
the claim. Excerpts are a deliberate context limit: the verifier cannot search
for missing code. A caller contract or API-version assumption without supplied
evidence should produce `insufficient_evidence`.

Optional `expected_verdict`, `label_notes`, and `origin_url` support evaluation.
Only the normalized finding and fetched excerpts enter the model input; labels,
notes, and origin links do not. Labels are human judgments, not model evidence.

The verifier returns `confirmed`, `rejected`, or `insufficient_evidence` with a
rationale, failure scenario, explanation of the introduced behavior, contrary
evidence considered, and source citations. Each explanation and quote is limited
to 2000 characters; citations should use the smallest supporting line range.
Python checks citation IDs, exact quotes, line ranges, and absence claims.
Excerpts shown to the model carry absolute line-number prefixes; quotes must
exclude those prefixes and preserve the original source text. Verified base
absence establishes a new path, but reachable behavior still needs evidence.
Confirmation also requires both
revisions and a cited changed file; rejection requires head evidence. Invalid
citations downgrade a model verdict to `insufficient_evidence` while retaining
`model_verdict`, `validation_errors`, and structured `citation_diagnostics`.
Matching citations establish evidence
integrity, not the truth of the model's causal reasoning.

## Results and evaluation limits

Artifacts retain the candidate, revision/blob IDs, excerpt ranges, prompt/code/
input hashes, requested model, elapsed time, and verification result. They omit
full source excerpts and raw provider errors, and redact the supplied GitHub and
model credentials from free-text values. Keys, schema enums, source IDs, and
Git/content hashes remain intact even with short dummy API keys. Reasoning and
cited code are retained: keep artifacts private and inspect them before sharing.
Record `pi --version` alongside experiment
results; the artifact does not capture effective provider settings or cost.

`status: failed` records a preparation or verifier failure with its stage and
exception class, plus the same sanitized `error_category` and schema
`validation_code` used in [live reports](pi_review_verification.md#bounds-and-reports).
It is distinct from a rejected finding or a completed empty
review. Exit 0 means a completed replay, including an insufficient-evidence
result; exit 1 means the run failed. `matches_expected` is a comparison with the
optional label, not a CI pass/fail gate, and is null for failed runs.

The three seed cases cover a refuted absence claim and a source-adjudicated
original/fixed test mismatch. They are not a precision benchmark. Before enabling
a live gate, add independently labeled bugs and false positives, keep every
revision of one PR/root cause in the same evaluation partition, repeat stochastic
runs, and report confirmed-bug retention alongside precision, abstentions,
failures, and latency. Fixed findings must stop reproducing. Inspect unseen
verifier output as well as known cases to avoid overfitting these fixtures.

The deterministic regression fixture
`tests/fixtures/pi-review-verification/pr215-citation-mismatches.json` freezes
three invalid citations from [run 35827083130](https://github.com/sii-system/agent-fleet/actions/runs/35827083130)
with exact Git source excerpts. It tests citation diagnostics, not a labeled
bug verdict, and is not a CLI replay case. Format/schema failure tests use
synthetic responses: [run 35832847898](https://github.com/sii-system/agent-fleet/actions/runs/35832847898)
retained only `PiResponseFormatError`, so its exact response failure cannot be
reconstructed.
