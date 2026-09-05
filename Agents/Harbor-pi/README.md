# Harbor Pi

Pi task-container integration for the shared Harbor runner.

Run it through Harbor common:

```bash
AGENT=pi \
DATASET_NAME=terminalbench21 \
MODEL=your-model-id \
BASE_URL=https://your-openai-compatible-endpoint \
API_KEY=sk-fake \
OPIK_URL= \
bash Agents/utils/common/Harbor/start.sh
```

The runner prepares the pinned `@earendil-works/pi-coding-agent` package as a
fully installed runtime archive plus a portable Node runtime once on the host.
Each Harbor task container prefers that pinned Node runtime over any Node binary
included by the task image, extracts the read-only Pi artifact, writes an
isolated provider configuration, and passes the task instruction over stdin.
Pi JSONL events are recorded in `/logs/agent/pi.txt`. The API key is passed only
through `AGENT_FLEET_API_KEY`; generated JSON contains its environment-variable
reference, not the credential.

Defaults are `PI_VERSION=0.81.1` and `PI_THINKING_LEVEL=high`. The model
gateway provider is derived from `BASE_URL` (matching the benchmarked
agent's gateway); `PI_PROVIDER` only overrides it when a distinct name is
needed. Override them in `config.local.env` or the calling environment.

Direct users of the Harbor adapter retain the legacy `provider/model` form
when `PI_PROVIDER` is absent. Set `PI_PROVIDER` explicitly when a direct-use
model ID contains `/` as part of its opaque gateway name; the shared runner
already derives and injects it before invoking the adapter.

## Extensions

Harbor can load local Pi `.ts` extensions for a task run. Put one or more
extension files directly in the ignored local directory
`Agents/Harbor-pi/extensions/`; each file is mounted read-only into the task
container and passed to Pi with `--extension`. The directory is intentionally
not version controlled, so benchmark runs that use extensions should record
the extension revision alongside their run output.

```bash
mkdir -p Agents/Harbor-pi/extensions
cp /path/to/my-extension.ts Agents/Harbor-pi/extensions/

AGENT=pi DATASET_NAME=terminalbench21 MODEL=your-model-id \
  BASE_URL=https://your-openai-compatible-endpoint API_KEY=sk-fake \
  bash Agents/utils/common/Harbor/start.sh
```

To use another host directory, set `PI_EXTENSION_SOURCE` to its absolute path
in `config.local.env` or the launch environment. `PI_EXTENSION_DIR` changes
the container mount point and normally remains `/opt/tb-pi/extensions`. An
empty directory loads no extensions.

Extensions require `HARBOR_ENVIRONMENT_TYPE=docker` or `opensandbox`, because
the pinned Pi runtime and extension files are supplied as host bind mounts.
Pi on E2B and QZ is rejected before a run starts. The monitor lists discovered
extension filenames and the container mount path so the active configuration is
visible in the run console.

Structure details: [STRUCT.md](./STRUCT.md)
