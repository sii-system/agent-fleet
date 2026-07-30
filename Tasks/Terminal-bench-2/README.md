# Terminal-bench-2

Terminal-Bench task lists.

```text
harbor_terminalbench21_tasks.txt
harbor_terminalbench21_mimocode_11_tasks.txt
tb_tasks.txt
```

`harbor_terminalbench21_tasks.txt` is used by `Agents/utils/common/Harbor`.
Use it through `Agents/utils/common/Harbor/start.sh` by setting
`DATASET_NAME=terminalbench21` and `DATASET_PATH` in
`Agents/utils/common/Harbor/env.sh`.

`harbor_terminalbench21_mimocode_11_tasks.txt` is the fixed 11-task subset
used for Mimo Max comparisons. Select it explicitly with `TASK_SOURCE_FILE`;
it is not a new `DATASET_NAME` alias.

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
