# DeepSearchQA

Agent Fleet runs the published [Harbor Hub dataset](https://hub.harborframework.com/datasets/kgmon/deepsearchqa) without vendoring its 900 tasks. `DATASET_NAME=deepsearchqa` resolves to `kgmon/deepsearchqa`; versioned forms such as `deepsearchqa@<version>` and `kgmon/deepsearchqa@<version>` also work.

DeepSearchQA tasks require internet access. For Claude Code runs, the alias leaves `WebSearch` and `WebFetch` available while continuing to disable interactive and remote-trigger tools. An explicit `HARBOR_DISALLOWED_TOOLS` value always takes precedence.

## Judge endpoint

Agent Fleet replaces the package's Gemini verifier at runtime with an OpenAI-compatible chat-completions verifier. Configure the existing judge endpoint values in the git-ignored `config.local.env`:

```bash
JUDGE_BASE_URL=https://judge.example/v1/chat/completions
JUDGE_API_KEY=replace-with-your-key
JUDGE_MODEL=your-judge-model
```

These values are consumed only by the verifier and are not passed to the evaluated agent. Live runs fail before setup if any value is missing. Scores from a judge other than the package's original Gemini judge may not be directly comparable with the published benchmark.

## Run

Start with one task because both web research and judge calls can incur cost:

```bash
DATASET_NAME=deepsearchqa \
HARBOR_LIMIT=1 \
HARBOR_N_CONCURRENT=1 \
bash Agents/utils/common/Harbor/start.sh --detach
```

For a full run, remove `HARBOR_LIMIT` and set the desired concurrency:

```bash
DATASET_NAME=deepsearchqa \
HARBOR_N_CONCURRENT=10 \
bash Agents/utils/common/Harbor/start.sh --detach
```

The native registry summary reports `mean_reward` from DeepSearchQA's primary `reward` metric and preserves the complete Harbor statistics in `OUTPUT_PATH/summary.txt`.
