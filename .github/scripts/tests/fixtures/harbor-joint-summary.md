# Benchmark Run Summary

Run ID: `fixture-run`

## Summary

The automated narrative summary is unavailable; deterministic results are shown below.

## Harbor

| Metric | Value |
| --- | --- |
| status | complete |
| RUN_ID | fixture-run |
| AGENT | pi |
| total | 2 |
| completed | 2 |
| mean_reward | 0.5 |

Rewards describe model outcomes; completed trials can have zero reward.

<details>
<summary>Original Harbor report (summary.txt)</summary>

```text
status: complete
RUN_ID: fixture-run
AGENT: pi
total: 2
completed: 2
mean_reward: 0.5
```

</details>

### Run Overview

| Metric | Value |
| --- | ---: |
| Runtime | 2m 0s |
| Success rate | 50.00% (1/2) |
| Failure rate | 50.00% (1/2) |

## Analyzer

### Analyzer Findings

| Finding | Tasks |
| --- | ---: |
| Analysis complete | 1 |
| Environment failure | 0 |
| Infrastructure failure | 1 |
| Model failure | 0 |
| Unknown root cause | 0 |
| Analysis failed | 0 |

### Analysis Summary

- **Infrastructure failure - `fixture-task`:** A required dependency was unavailable.

### Recommended Actions

No additional action was generated.

## Fixer Results

Smoke verification passed for one sampled task; a full rerun is pending.

### What Fixer Changed

Restored the missing dependency.

### Remaining Issues

Full benchmark verification remains pending.

Full Fixer report: [fix-report-latest.md](fixer/fix-report-latest.md)
