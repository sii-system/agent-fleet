# YiCloud OpenSandbox Quick Start

This guide runs a local Harbor task in a YiCloud OpenSandbox instance. The
runner builds or reuses the task image, uploads the agent runtime, executes the
agent and verifier, collects the result, and deletes the instance.

## Prerequisites

- Run `./scripts/setup.sh` once from the repository root.
- Install Docker with Buildx and log in to the target image registry.
- Prepare a local Harbor dataset whose tasks contain an
  `environment/Dockerfile`.
- Obtain YiCloud API credentials, a project name, and an OpenSandbox
  environment ID.
- Make the model gateway reachable from OpenSandbox.
- For the S3 upload backend, provide an `s3cmd` configuration and a writable
  bucket.

OpenSandbox currently supports single-container tasks only. Docker Compose
tasks fail before an instance is created.

## Configure

Copy the committed configuration template, then keep all credentials in the
git-ignored local file:

```bash
cp config.env config.local.env
```

Set at least the following values in `config.local.env`:

```bash
BASE_URL=https://model-gateway.example.com
API_KEY=your-model-api-key
MODEL=your-model-id

RL_ENVIRONMENT_TYPE=opensandbox
YICLOUD_PUBLIC_KEY=your-yicloud-public-key
YICLOUD_SECRET_KEY=your-yicloud-secret-key
YICLOUD_PROJECT_NAME=your-project
YICLOUD_SANDBOX_ENVIRONMENT_ID=env-xxxxxxxx-xxx

HARBOR_OPENSANDBOX_REGISTRY=registry.example.com
HARBOR_OPENSANDBOX_IMAGE_REPOSITORY=project/benchmark-task-images
HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX=project/benchmark-task-images

YICLOUD_SANDBOX_UPLOAD_BACKEND=s3
YICLOUD_SANDBOX_S3_CONFIG=/absolute/path/to/s3cfg
YICLOUD_SANDBOX_S3_BUCKET=your-bucket
```

Use the immutable environment ID in automation. The runner rejects requests
without an explicit environment ID or exact environment name.

## Run One Task

Start with one worker and one task:

```bash
cd Agents/utils/common/Harbor

AGENT=claude-code \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
HARBOR_N_CONCURRENT=1 \
bash start.sh
```

The command prints the output and summary paths. For debugging, add
`YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL=1` and delete the retained instance after
inspection.

## Optional: Prebuild Task Images

For a batch run, publish task images once before starting workers:

```bash
set -a
source config.local.env
set +a

HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY=4 \
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Already published content-addressed images are reused. Unsupported Compose
tasks are listed as skipped in the prebuild report.

## Troubleshooting

- A scheduling timeout or long `Pending` state is a platform capacity issue;
  the log includes the Sandbox ID and latest status.
- An image preparation failure occurs before Sandbox creation. Verify Buildx,
  registry login, and the task Dockerfile.
- An upload failure should be diagnosed separately from agent execution. Check
  S3 credentials, bucket access, DNS, and the signed download URL.
- A model request failure means the instance started, but its configured model
  gateway is unreachable or rejected the request.

See [task image management](OPENSANDBOX_IMAGE_MANAGER.md) for image naming,
caching, and registry internals. See [Harbor structure](STRUCT.md) for the full
configuration reference.
