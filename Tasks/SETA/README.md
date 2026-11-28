# SETA

SETA task lists.

```text
harbor_tasks.txt
terminal_bench_tui_tasks.txt
```

`harbor_tasks.txt` is used by `Agents/utils/common/Harbor`.
`terminal_bench_tui_tasks.txt` is a legacy task list kept for compatibility.

Use `harbor_tasks.txt` for local runs with
`DATASET_NAME=auto DATASET_PATH=/data/seta/Harbor-Dataset bash Agents/utils/common/Harbor/start.sh`.
The `seta` alias selects the Harbor registry dataset instead.

Optional online analysis:

```bash
DATASET_NAME=seta
HARBOR_ONLINE_ANALYSIS=1
```

With `DATASET_NAME=seta`, online analysis tails Harbor `jobs/**/job.log`
files and reports deterministic Harbor environment signals.

Optional early stop for task-blocking SETA signals:

```bash
DATASET_NAME=seta
HARBOR_ONLINE_ANALYSIS=1
HARBOR_EARLY_STOP=1
```

When enabled, a worker stops its current task if online analysis reports a
matching `task_blocking=true` SETA event for that task.

Outputs:

```text
${OUTPUT_PATH}/online-analysis/environment-events.jsonl
${OUTPUT_PATH}/online-analysis/environment-summary.json
${RUNTIME_DIR}/online-rule-analyzer.log
${RUNTIME_DIR}/online-rule-analyzer.pid
```
