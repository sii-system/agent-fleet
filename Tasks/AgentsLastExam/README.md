# Agents' Last Exam → Harbor

This adapter runs the Linux and Windows CPU tasks from the [official ALE repository](https://github.com/rdi-berkeley/agents-last-exam/tree/d9abc0734b56ea34116c5bfcbdd0b808269ab9e2)
at `d9abc0734b56ea34116c5bfcbdd0b808269ab9e2`: **205 variants from 151 task
implementations (134 Linux, 71 Windows variants)**. Both licensed and unlicensed
CPU snapshots are included. GPU snapshots are excluded before importing task
modules. Native task prompts, setup and graders remain unchanged. Generated
tasks stay outside this repository.

Harbor owns concurrency, trials, timeouts, retries, resume and result reporting.
`ALEEnvironment` provisions a fresh sandbox per trial: the existing SBX (qz) sandbox
provider by default, with Docker fallback for Ubuntu and a KubeVirt PVC clone for Windows. ALE's `TaskDriver`
sets up and grades the same guest that the Harbor agent uses. `ALEVerifier`
stages references only after agent execution, preserves zero, fractional and
negative scores, and saves the native result. Missing scores and grader failures
are errors. Generated shell/batch verifiers fail if the custom verifier is omitted.

## Prepare

Use the same setup/run workflow as WAA and WAA-V2. Setup prepares a Python 3.12
Harbor environment with `uv sync`, the pinned native ALE source and dependencies,
and all CPU Harbor task variants:

```bash
./Tasks/AgentsLastExam/setup.sh
```

`HARBOR_ALE_CACHE_DIR` defaults to `$HOME/.cache/agent-fleet/ale`, and
`HARBOR_ALE_ENV_DIR` defaults to `$HARBOR_ALE_CACHE_DIR/venv`. Native dependencies
stay in `$HARBOR_ALE_CACHE_DIR/native-env` so they cannot change Harbor's pins.
Setup reuses a matching dataset and records the generated local `uv.lock` hash
and installed package versions. Startup validates these and never installs packages.
Use `--source`, `--env-dir` (or `--native-env-dir`) and `--dataset` during setup
to reuse existing prepared locations. The lockfile is generated and git-ignored.

Setup installs upstream framework and evaluation dependencies, including large
packages such as PyTorch with CPU host wheels. The source revision and native
CUA dependency are pinned; other dependencies follow upstream ranges. Conversion
and launch reject different revisions or changed native code. Regenerate into a
new output directory; old Windows-only datasets must be converted again.
Startup never installs host dependencies.

## Guest images and task data

For Linux, use a prepared ALE Ubuntu container image with native task software,
input data and encrypted references. The upstream image is
`agentslastexam/ale-ubuntu22-docker:latest`, exported from the native Ubuntu
image with the same `/media/user/data/agenthle` and `/opt/ale-run/.venv` paths.
Supply an immutable prepared tag/digest for repeatable runs.

The default `--linux-backend auto` prefers the repository's existing
[SBX/qz provider](../../Agents/utils/common/Harbor/QZ_SANDBOX_README.md).
Configure `SBX_API_KEY` (or `QZ_SANDBOX_API_KEY`) and a prepared ALE template,
using `sbx_template` in the image map or `QZ_SANDBOX_TEMPLATE` /
`QZ_SANDBOX_TEMPLATE_MAP`. Templates must contain the native task software/data
and a running CUA server on port 5000. A profile may supply `startup_command`
to start prepared guest services. Template CPU/RAM must satisfy task requirements;
SBX uses its registered compute spec and cannot enforce per-task resource limits.

If SBX is unconfigured or sandbox provisioning fails, auto mode cleans up the
SBX attempt and uses Harbor's existing Docker environment. `--linux-backend sbx`
requires SBX and never falls back; `--linux-backend docker` forces Docker. Once
native setup starts, setup/agent/grader failures remain trial errors and never
trigger backend switching. Provisioning cancellation also never triggers fallback.

Docker requires the usual Docker/Compose host setup. It boots the prepared
container with ALE's `/dockerstartup/entrypoint.sh`, preserving its desktop and
CUA services. There is no QEMU, qcow2 disk, or `/dev/kvm` requirement. CPU/memory
limits follow the Harbor task configuration. Native setup/grading reach CUA
through a per-trial loopback HTTP proxy using the backend's existing command/file
APIs; no guest port needs to be exposed externally.

All Linux tasks remain included, including nested Docker and Apptainer/Singularity
tasks. Prepare an image/template with those runtimes and the required permissions.
For a Docker host that supports them, set `docker.privileged: true` and
`docker.enable_dind: true` to start ALE's baked inner Docker daemon. An image
containing the required nested workloads/GUI bundles is still necessary. The
adapter never skips tasks because their runtime is missing; native failures are
reported as trial errors.

For Windows, import the official ALE CPU images as golden PVCs following the
[Windows backend guide](../../Agents/utils/common/Harbor/KUBEVIRT_WINDOWS_README.md).
Preserve `E:\agenthle`, task applications/licenses, Python and CUA startup.
Alternatively, use KubeVirt's native copy-on-write layer over each task's mapped
read-only golden PVC (`HARBOR_KUBEVIRT_DISK_MODE=overlay`) to avoid per-trial
cloning; see the backend guide. Retained overlay trials stay running because a
platform-level VM stop discards the layer. Free/licensed PVC mappings still apply.

Create an operator-owned image map, for example `/data/ale-images.json`:

```json
{
  "cpu-free-ubuntu": {
    "image_family": "ale-ubuntu22",
    "sbx_template": "ale_ubuntu22_prepared",
    "docker_image": "your-registry/ale-ubuntu22:prepared",
    "docker": {"privileged": true, "enable_dind": true}
  },
  "cpu-free": {"pvc": "ale-cpu-free", "image_family": "ale-win10"},
  "cpu-license": {"pvc": "ale-cpu-license", "image_family": "ale-win10"}
}
```

Each snapshot must match the native task's software and licensing requirements.
CPU, memory, task timeout and Windows desktop resolution follow native task
metadata. GPU tasks and GPU resource overrides are rejected.

Windows setup fails if the guest cannot apply the profile's requested resolution.
Prepare an image/display driver supporting that mode before benchmark runs.
`ALE_WINDOWS_RESOLUTION_STRICT=0` explicitly allows diagnostic runs at the guest's
current mode; the warning and `ale-setup.json` record that the requested resolution
was not applied. Results from this override do not establish benchmark parity.

Golden images must contain encrypted `reference.7z` archives only, with no
plaintext references for **any** variant. Keep judge credentials on the host.
Default `baked_in_sandbox` staging uses native encrypted-reference handling;
set `ALE_REFERENCE_ARCHIVE_PASSWORD` on the host for grading. Native `gs://`,
`s3://` and `oss://` task staging are supported via `--task-data-source`, with
their native guest tools/credentials prepared beforehand. Inputs/software are
staged before setup; references are staged only during verification. Host
`local:` and the unimplemented `hf://` task-data backend are rejected.
`AGENTHLE_CREDENTIALS_DIR` and `AGENTHLE_EVAL_CREDENTIALS_DIR` follow native ALE
contracts. Keep secrets outside the image map and generated dataset.

## Run

The default `Agents.AgentsLastExam.agent:ALECommandAgent` uses an image-provided
entrypoint on each OS. Both entrypoints read UTF-8 `HARBOR_INSTRUCTION_FILE` and
select `HARBOR_MODEL`. Provide CLI/GUI tools suitable for the tasks. The Windows
side reuses [WindowsCommandAgent and its optional preparation manifest](../../Agents/utils/common/Harbor/KUBEVIRT_WINDOWS_README.md#harbor-and-agent-contracts).
Linux entrypoints run in bash and write logs under `/logs/agent`; Windows logs
use `C:/logs/agent`.

```bash
export SBX_API_KEY='sbx_your-private-key'
export HARBOR_ALE_LINUX_AGENT_COMMAND='/opt/agent/run-agent.sh'
export HARBOR_KUBEVIRT_IMAGE=ale-cpu-free
export HARBOR_WINDOWS_AGENT_COMMAND='C:\Agent\run-agent.cmd'
export ALE_REFERENCE_ARCHIVE_PASSWORD='your-private-reference-password'
export HARBOR_ALE_IMAGE_MAP=/data/ale-images.json
./Tasks/AgentsLastExam/run.sh --all --dry-run
./Tasks/AgentsLastExam/run.sh --all --workers 2 --model your-model --output ./runs/ale-cpu
# The unified launcher, FleetSpec and prompt mode use the same benchmark runner:
./scripts/run_fleet.sh --taskset ale --workers 2
# Subsets and ordinary Harbor options:
./Tasks/AgentsLastExam/run.sh --domain visual_media --workers 2
./Tasks/AgentsLastExam/run.sh --task computing_math/tris_crackme -- --max-retries 2
./Tasks/AgentsLastExam/run.sh --all --os linux --dry-run
```

The default agent alias is `ale-command`. Use `--agent module:Class` before `--`
for a custom Harbor agent. Mixed runs need
an agent supporting both OSes; Linux-only agents can run with Windows tasks
filtered out. Configure kubeconfig, namespace, guest ports, nodes and clone
storage through shared `HARBOR_KUBEVIRT_*` settings. Each Windows trial replaces
the initial preflight image with its mapped PVC. Linux trials do not require
cluster settings. Runtime overrides, including empty values, follow the shared
configuration loader. `OPIK_URL` selects the prepared `opik harbor` runner;
empty selects ordinary Harbor.

Select `--all`, `--domain DOMAIN`, or `--task DOMAIN/TASK` (comma-separated task
names are supported). A native task selects all its variants; use
`<domain>--<task>--v<variant-index>` to select one variant. Add `--os linux|windows`
to restrict the OS. Unknown or filtered task names fail before launching Harbor.
Dry-run validates full dataset provenance and selected image mappings, prints
counts by OS and selected names, and starts no guests.
The existing explicit `--dataset`, `--source`, `--native-python`, `--image-map`
options and forwarded Harbor `--include-task-name` / `--exclude-task-name` remain
available. FleetSpec/prompt mode support `ale`, `ale-command` and custom Harbor
agents; unified `--output` saves a FleetSpec, while the benchmark runner's
`--output` chooses the Harbor job directory. Both runners stay in the foreground.
Forwarded CLI options can contain credentials and are never printed.

Results use normal Harbor job/trial artifacts plus `verifier/native-result.json`.
Both OSes write `ale-setup.log` and `ale-evaluate.log`; `ale-linux.json` records
the selected Linux backend. Resume with
`$HARBOR_ALE_ENV_DIR/bin/harbor jobs resume --job-path ./runs/ale-cpu`
(default environment: `$HOME/.cache/agent-fleet/ale/venv`) and the same configuration and
`PYTHONPATH=.:Tasks/AgentsLastExam:Agents/utils/common/Harbor`.
Cancellation stops native phases before guest cleanup. Hard termination may
leave resources: inspect `kubevirt.json` for Windows and `ale-linux.json` for
Linux. Inspect Docker's Harbor project or the SBX sandbox using its backend logs.

## Checks

```bash
PYTHONPATH=.:Tasks/AgentsLastExam:Agents/utils/common/Harbor \
  python3 -m unittest discover -s Tasks/AgentsLastExam/tests -v
# Also exercise native ALE setup/grading through real Harbor trials on both OSes:
ALE_TEST_SOURCE=/path/to/pinned/ale ALE_TEST_PYTHON=/path/to/native-env/bin/python \
  PYTHONPATH=.:Tasks/AgentsLastExam:Agents/utils/common/Harbor \
  python3 -m unittest discover -s Tasks/AgentsLastExam/tests -v
```

Portable and loopback tests do not establish live benchmark parity. A complete
run needs a prepared ALE SBX template or Docker image, Windows CPU/licensed images, native task
data, judge credentials and agent entrypoints. These are operator-provided assets.
