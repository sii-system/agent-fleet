# Terminal-bench-2

Terminal-Bench task lists.

```text
harbor_terminalbench21_tasks.txt
tb_tasks.txt
```

`harbor_terminalbench21_tasks.txt` supplies task selection for local runs through
`Agents/utils/common/Harbor`. Use `DATASET_NAME=auto DATASET_PATH=/data/terminalbench21/tasks`
with `bash Agents/utils/common/Harbor/start.sh`.
The `terminalbench21` alias selects the Harbor registry dataset instead.

Optional online analysis:

```bash
DATASET_NAME=terminalbench21
HARBOR_ONLINE_ANALYSIS=1
```

With `DATASET_NAME=terminalbench21`, online analysis tails Harbor
top-level `*.console.log` files and reports deterministic task-status
signals.

Outputs:

```text
${OUTPUT_PATH}/online-analysis/environment-events.jsonl
${OUTPUT_PATH}/online-analysis/environment-summary.json
${RUNTIME_DIR}/online-rule-analyzer.log
${RUNTIME_DIR}/online-rule-analyzer.pid
```
