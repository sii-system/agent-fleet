# QZ Template Manager

`qz_template_manager.py` creates a QZ sandbox Template from an image that is
already available to the platform. It is a standalone, Python-standard-library
tool; it does not build or push images and does not depend on Harbor's QZ
sandbox provider.

## Configure

Set a Sandbox API key. QZ-specific variables take precedence over their
platform aliases:

```bash
export QZ_SANDBOX_API_KEY=sbx_example
# or: export SBX_API_KEY=sbx_example
```

For compatibility with the QZ runtime, `E2B_API_KEY` is also accepted only
when it has the QZ-specific `sbx_` prefix; cloud E2B keys are rejected.

The default API URL is `https://qz-sbx-api.sii.edu.cn`. Override it with
`QZ_SANDBOX_API_URL` or `SBX_API_URL`; the manager adds `/v1` when needed.

## Create a Template

```bash
cd Agents/utils/common/Harbor

python qz_template_manager.py create \
  --name harbor_task_demo \
  --image docker.sii.shaipower.online/example/task:tag \
  --spec g.c1 \
  --image-source official
```

Template names may contain only ASCII letters, digits, and underscores.

The command reserves the Template, binds the image, waits for the build to
reach `ready`, and writes only the Template ID to stdout. Progress and errors
go to stderr, so the result can be passed directly to the QZ provider:

```bash
QZ_SANDBOX_TEMPLATE="$(
  python qz_template_manager.py create \
    --name harbor_task_demo \
    --image docker.sii.shaipower.online/example/task:tag \
    --exists-ok
)" || exit 1
export QZ_SANDBOX_TEMPLATE
```

Creation refuses to touch an existing Template. `--exists-ok` returns an
existing Template only when its latest build is already `ready`; it never
rebuilds it. Concurrent creation of the same name is outside this first
version's scope.

## Inspect Templates

```bash
python qz_template_manager.py list
python qz_template_manager.py get --name harbor_task_demo
```

Both inspection commands print JSON. This version intentionally has no
rebuild or delete command. `official` is the verified `imageSource` for
platform images; other values are passed through for later validation with
custom registries.

For benchmark inventory, explicit one-task materialization, and runtime
selection, see [QZ Template Mapping](QZ_TEMPLATE_MAPPING.md). Normal benchmark
runs only resolve ready Templates and never call the create path.
