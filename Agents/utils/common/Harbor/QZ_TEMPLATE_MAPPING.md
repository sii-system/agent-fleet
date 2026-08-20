# QZ Template Mapping Inventory

`qz_template_mapping.py` inventories the prebuilt images declared by local
Harbor tasks and writes deterministic input for the per-task QZ Template
resolver. Inventory makes no QZ API calls and creates no Templates.

## Inventory a benchmark

Point the tool at a directory whose immediate children are Harbor tasks:

```bash
cd Agents/utils/common/Harbor

python qz_template_mapping.py \
  --dataset-root /workspace/terminal-bench-2-1/tasks \
  --benchmark terminalbench21 \
  --task-list ../../../../Tasks/Terminal-bench-2/harbor_terminalbench21_tasks.txt \
  --spec g.c1 \
  --output /tmp/terminalbench21-qz-templates.json
```

Without `--task-list`, every immediate child containing `task.toml` is
inventoried. `--dataset-root` may also point directly at one task. The command
fails without writing a partial mapping if any selected task lacks a non-empty
`environment.docker_image`.

## Schema v1

The output is deterministic: it contains no generation timestamp, absolute
dataset path, credentials, or live platform state.

```json
{
  "benchmark": "terminalbench21",
  "identity_version": "qz-template-image-v1",
  "schema_version": 1,
  "tasks": {
    "adaptive-rejection-sampler": {
      "docker_image": "example/task-image:tag",
      "template_key": "sha256:..."
    }
  },
  "templates": {
    "sha256:...": {
      "image": "example/task-image:tag",
      "image_source": "official",
      "spec": "g.c1",
      "template_id": null,
      "template_name": "af_task_image_tag_..."
    }
  }
}
```

`template_key` is SHA-256 over canonical JSON containing
`identity_version`, the exact image reference, `image_source`, and QZ spec.
Tasks with the same tuple share one Template entry. Changing any member creates
a new key and alias. Aliases contain only letters, digits, and underscores.

The exact image string is intentionally preserved. Digest-qualified image
references are preferred because mutable tags can move while retaining the
same v1 key. Resolving a registry tag to a manifest digest is outside this
inventory phase.

## Resolve or materialize one task

`qz_template_resolver.py` consumes the mapping one task at a time. Benchmark
runs are read-only: they resolve the cached ID or deterministic alias through
the live QZ API and reject missing, non-ready, or identity-mismatched
Templates. The live Template must expose the mapping's content-derived alias
and QZ spec. They never create a Template implicitly.

Before a live `resolve`, `bind`, or `materialize` command, either export the QZ
API variables or load the repository-local configuration:

```bash
source ./env.sh
```

Resolve one task without changing the mapping or platform:

```bash
python qz_template_resolver.py resolve \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler
```

Bind an existing ready Template that has the mapping's deterministic alias and
QZ spec:

```bash
python qz_template_resolver.py bind \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler \
  --template-id existing_template_id
```

Explicitly create or reuse only one task through Template Manager v1:

```bash
python qz_template_resolver.py materialize \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task adaptive-rejection-sampler
```

## Materialize an explicit task batch

Use the batch tool only during Template preparation. It requires an explicit
task list and intentionally has no `--all` mode:

```bash
python qz_template_batch_materialize.py \
  --mapping /path/to/terminalbench21-qz-templates.json \
  --task-list /path/to/selected-tasks.txt \
  --workers 8
```

The tool resolves the selected tasks before making QZ API calls, groups them by
`template_key`, and runs at most `--workers` unique Template operations at once.
Mapping writes stay serialized in the main process, so successful IDs are saved
atomically without concurrent workers overwriting each other.

Failures are isolated per unique Template. The command writes a JSON result to
stdout and exits non-zero if any Template failed. Rerunning the same command
reuses IDs already saved in the mapping and ready deterministic aliases.

The write commands record `template_id` only after live status, deterministic
alias, and QZ spec validation succeed. QZ's read API does not expose the source
image, so the server-returned alias is the live content-identity commitment: it
is derived from the exact image reference, image source, and spec in the
mapping. A legacy Template without that alias must be materialized under the
deterministic name instead of being bound by ID.

Enable per-task selection in the runner with:

```bash
QZ_SANDBOX_TEMPLATE_MAP=/absolute/path/to/terminalbench21-qz-templates.json
```

Set either `QZ_SANDBOX_TEMPLATE_MAP` or the backward-compatible fixed
`QZ_SANDBOX_TEMPLATE`, never both. The mapping is a cache, not the platform
fact source: every reuse is checked through the live API.

Regenerating an existing output preserves a resolved `template_id` only when
its full `template_key` is unchanged. The resolver never rebuilds or deletes a
same-name Template automatically.

## Acceptance sequence

1. Inventory the selected Terminal-Bench task list and report selected task
   count, unique image count, and any task that cannot be represented.
2. Use `adaptive-rejection-sampler` as the first real single-task acceptance;
   its mapped Template must reach ready before one Oracle trial runs.
3. Select a second task with a different `template_key`; run both sequentially
   and verify each receives its own Template ID, reward is recorded, exceptions
   are zero, artifacts exist, and temporary Sandboxes are deleted.
4. Only after those pass, run repeated trials of one task to verify Template
   reuse, then a mixed-image small batch before increasing concurrency.

This phase does not build or push Docker images. Add that workflow only for a
real selected task whose image is not already available to QZ Template build.
