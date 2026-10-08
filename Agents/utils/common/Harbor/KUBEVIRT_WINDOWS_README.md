# WAA and ALE Windows images on KubeVirt

This backend implements Harbor 0.18.0's `BaseEnvironment` using an isolated
Windows VM per trial. Supported guest protocols are WindowsAgentArena (WAA),
including WAA-V2's Windows 11 snapshot, and Agents' Last Exam (ALE)'s Windows
CUA computer-server. ALE support covers Windows tasks. The Harbor controller runs on Linux. Benchmark tasks,
datasets, setup logic, evaluators, and scoring belong to the consuming project.
The WAA and WAA-V2 Harbor benchmark adapters are available under
[Tasks/WindowsAgentArena](../../../../Tasks/WindowsAgentArena/README.md), including
task setup, PC-Agent/custom agents, native evaluation, resume and reporting.
ALE's full benchmark adapter is not included.

Use `run_kubevirt_windows.sh` for Windows runs. It uses the existing config
loader and pinned runner validation, but avoids the Linux dependency installers,
bind mounts, and agent wrappers used by `start.sh` / `run_fleet.sh`. Those unified
launchers dispatch full Harbor WAA benchmarks via `--taskset waa` or `--taskset waa-v2`. Other Windows
Harbor runs use the dedicated launcher. The backend is also importable
directly by another project's Harbor configuration.

## KubeVirt-native VM lifecycle

The control plane follows KubeVirt's own API surface and talks **only** to the
kube-apiserver over HTTPS; it never shells out to `kubectl`, `virtctl`, or a
client library. Each trial is a fresh `VirtualMachine` (`kubevirt.io/v1`) that
owns a **CDI DataVolume clone** of a golden-image PVC in the same namespace.
`HARBOR_KUBEVIRT_IMAGE` identifies that source PVC. Each VM attaches only its
unique `<vm-name>-root` clone; task writes never reach the golden image or another
trial. KubeVirt manages clone creation through `spec.dataVolumeTemplates` and
waits for it before booting. CDI derives the disk size from the source PVC.
Stopping a retained VM preserves its clone; deleting the VM garbage-collects
its DataVolume and PVC. The storage class's reclaim policy controls removal of
the underlying storage.

The cluster must have CDI and a provisioner/storage profile capable of PVC
cloning. `HARBOR_KUBEVIRT_STORAGE_CLASS` selects target storage; when omitted,
CDI uses the cluster's default virtualization/storage class. See the
[CDI storage and PVC cloning contract](https://github.com/kubevirt/containerized-data-importer/blob/main/doc/datavolumes.md).
`HARBOR_KUBEVIRT_NODE` is an optional scheduling constraint for node-local storage.

- Create: `POST /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines`
- Read status + pod IP: `GET .../virtualmachineinstances/{name}` (`status.interfaces[].ipAddress`)
- Start/stop: `PUT /apis/subresources.kubevirt.io/v1/.../virtualmachines/{name}/start|stop`
- Delete: `DELETE /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}`

The VM spec matches a proven Windows guest: SATA root disk (the golden image
carries no virtio storage driver), masquerade pod network with the guest HTTP port
declared, EFI secure boot, and cloud-init. The runner lives outside the pod
overlay, so the backend additionally creates a **NodePort `Service`** that maps
the guest port onto a node IP the runner can reach, and connects the guest
transport to `node_ip:node_port`.

## Host and image requirements

- Prepare the pinned Harbor runner with repository setup; workload startup only
  validates it and does not install Windows tools.
- Confirm the runner can reach the kube-apiserver (`HARBOR_KUBEVIRT_API_SERVER`
  or the kubeconfig's `server`) with mTLS, and can reach node ports on a cluster
  node (the NodePort Service exposes the guest server there). No SSH/SFTP, WinRM, or client
  tools are used.
- Provide a kubeconfig (`HARBOR_KUBEVIRT_KUBECONFIG`, default `~/.kube/config`)
  whose current user presents client certificate + key with permission to
  create/get/start/stop/delete `virtualmachines`, get `virtualmachineinstances`,
  and create/get/delete `services` in the namespace, plus get `nodes` for guest
  endpoint resolution. CDI must authorize cloning from the source PVC (including
  `datavolumes/source` creation permission where required by CDI).
  Alternatively set `HARBOR_KUBEVIRT_API_SERVER` (the kubeconfig is still read
  for the mTLS credentials). Token auth is not supported.
- Import a clean, shut-down Windows image into a golden-image PVC in the trial
  namespace. Set `HARBOR_KUBEVIRT_IMAGE` to that PVC's name. Keep it immutable and
  unattached to running VMs while cloning; publish a new PVC for each image version.
  Host file paths are rejected, because attaching them directly would share writes.
- For the default `waa` protocol, preserve the image's WAA service and logged-in session startup. WAA's
  [setup script](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/setup.ps1#L380-L429)
  opens port 5000 and registers the server at logon. The backend waits for
  `/probe`, then uses `/execute`, `/setup/upload`, and `/file` from the
  [guest server](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/server/main.py).
  A running VM without a running guest server is insufficient. For `ale`,
  preserve the CUA computer-server instead; see the ALE contract below.
- The image must boot with the node's disk/network devices and VirtIO drivers.
  Windows PowerShell 5.1+ and the guest server account must be able to create
  `C:/ProgramData/AgentFleet`, `C:/logs`, and the workspace. Commands run as the
  guest server's account; user selection and automatic privilege escalation are
  unsupported. Agent tools can be supplied by the runtime preparation manifest
  below.

> **Validation boundary.** The WAA API contract is source-checked at the pinned
> revision above and exercised by local HTTP tests. Guest preparation and the
> PowerShell supervisor were validated on existing Windows guests (see live
> validation below). This revision replaces the custom platform HTTP API with a
> KubeVirt-native control plane, live-validated for VM create/start/read/delete,
> NodePort WAA exposure, and guest execution. Complete Harbor benchmark runs and
> production image management remain the consuming project's responsibility.
> ALE's Windows wire contract is source-checked and exercised by offline and
> loopback HTTP tests; no live ALE image or benchmark run has been validated.
> ALE Linux tasks, OSWorld, and other guest protocols remain outside this backend.

## ALE Windows guest contract

Set `HARBOR_KUBEVIRT_GUEST_PROTOCOL=ale` for an imported ALE Windows image.
The source PVC must include the applications and task data expected by the
selected Windows tasks. Preserve the CUA server's startup and interactive
Windows session, and allow its HTTP port through the guest firewall. Its account
must have the same workspace/log permissions described above. The KubeVirt
backend clones the image and waits for `GET /status` to return `{"status":"ok"}`.

ALE uses `POST /cmd` with `{"command":"run_command","params":{"command":"..."}}`.
Responses contain SSE `data:` JSON records, including when the content type is
`text/plain`. Successful commands supply `success`, `stdout`, `stderr`, and
`return_code`. The backend sends encoded PowerShell helper commands and uses the
same detached `execute.ps1` supervisor as WAA for cmd.exe semantics, per-command
working directories/environment, long runs, and process-tree timeout/cancellation.
It does not retry commands after ambiguous transport failures.

Binary transfers use CUA `write_bytes` (`path`, `content_b64`) and `read_bytes`
(`path`, `offset`, `length`; response `content_b64`). Uploads stage 1 MiB chunks,
append them with PowerShell, and publish the completed file. Downloads decode
bounded chunks into a local temporary file and replace the destination on
success. Empty files, Unicode paths, and binary contents are preserved. Both
transfers have a total deadline; upload staging cleanup is best effort.

The wire contract was checked against ALE's
[SandboxHandle](https://github.com/rdi-berkeley/agents-last-exam/blob/d10fb61a14f9719774c3520c5763068b28ef5546/ale_run/base_interface/sandbox.py)
and CUA's
[command dispatcher](https://github.com/trycua/cua/blob/0f29c142d7fe3e05ea0ce276cee11b3a9725ba01/libs/python/computer-server/computer_server/main.py)
and [file interface](https://github.com/trycua/cua/blob/0f29c142d7fe3e05ea0ce276cee11b3a9725ba01/libs/python/computer-server/computer_server/handlers/base.py).
This supports ALE's image-local CUA HTTP service without cloud-provider auth.
Controller credentials are never forwarded to the guest.

Example using externally adapted Windows Harbor tasks:

```bash
export HARBOR_KUBEVIRT_GUEST_PROTOCOL=ale
export HARBOR_KUBEVIRT_IMAGE=ale-win10-golden-v1
export HARBOR_KUBEVIRT_GUEST_PORT=5000
export HARBOR_WINDOWS_AGENT_COMMAND='C:\Agent\run-agent.cmd'

./Agents/utils/common/Harbor/run_kubevirt_windows.sh --dry-run \
  --path /data/ale-windows-harbor-tasks --n-concurrent 1
./Agents/utils/common/Harbor/run_kubevirt_windows.sh \
  --path /data/ale-windows-harbor-tasks --n-concurrent 1
```

The consuming project's adapter must select Windows tasks, declare
`[environment].os = "windows"`, stage their inputs, and provide Windows verifier
entrypoints/rewards. ALE's native task scripts and graders are not converted by
this backend. An agent entrypoint requiring desktop tools must configure its
CUA/MCP bridge inside the guest; the command-agent bridge invokes that entrypoint
using the existing request contract below.

## Configuration and launch

Put private settings in ignored `config.local.env` or exported environment
variables. Runtime environment values, including explicitly empty ones, override
saved configuration.

```bash
export HARBOR_KUBEVIRT_KUBECONFIG=$HOME/.kube/config
export HARBOR_KUBEVIRT_API_SERVER=https://10.254.64.34:6443
export HARBOR_KUBEVIRT_IMAGE=windows-golden-v1
export HARBOR_KUBEVIRT_NODE=cpu-nat-391
export HARBOR_KUBEVIRT_NAMESPACE=windows-benchmarks
export HARBOR_WINDOWS_AGENT_COMMAND='C:\Agent\run-agent.cmd'

./Agents/utils/common/Harbor/run_kubevirt_windows.sh --dry-run \
  --path /data/external-windows-tasks --n-concurrent 1

./Agents/utils/common/Harbor/run_kubevirt_windows.sh \
  --path /data/external-windows-tasks --n-concurrent 1
```

Dry-run makes no apiserver calls and deliberately does not print raw arguments,
which may include secrets. It does not validate the image or the kubeconfig.

Optional settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBOR_KUBEVIRT_KUBECONFIG` | `~/.kube/config` | kubeconfig with the mTLS client cert/key and cluster `server` |
| `HARBOR_KUBEVIRT_API_SERVER` | from kubeconfig | KubeVirt apiserver URL; overrides the kubeconfig `server` (certs still come from kubeconfig) |
| `HARBOR_KUBEVIRT_IMAGE` | required | Golden-image PVC name in the trial namespace |
| `HARBOR_KUBEVIRT_STORAGE_CLASS` | cluster default | Target storage class for per-trial clones |
| `HARBOR_KUBEVIRT_NODE` | empty | Optional node hostname constraint, for example for node-local storage |
| `HARBOR_KUBEVIRT_NAMESPACE` | `default` | Namespace for the VM and guest Service |
| `HARBOR_KUBEVIRT_DISK_BUS` | `sata` | Root disk bus (sata/virtio/scsi); Windows golden images need SATA |
| `HARBOR_KUBEVIRT_GUEST_PROTOCOL` | `waa` | `waa` or `ale`; ALE Windows CUA protocol |
| `HARBOR_KUBEVIRT_GUEST_PORT` | `5000` | Guest HTTP port; falls back to `HARBOR_KUBEVIRT_WAA_PORT` when unset |
| `HARBOR_KUBEVIRT_GUEST_NODE_PORT` | auto | Optional explicit nodePort; falls back to `HARBOR_KUBEVIRT_WAA_NODE_PORT` when unset |
| `HARBOR_KUBEVIRT_EXTRA_PORTS` | empty | Comma-separated auxiliary guest ports in the same owned Service; the WAA benchmark launcher defaults to `9222,8080` |
| `HARBOR_KUBEVIRT_START_TIMEOUT` | `1800` | Total create/boot/guest-readiness deadline, seconds. Windows boot can take ~10 min; keep this generous |
| `HARBOR_KUBEVIRT_COMMAND_TIMEOUT` | `3600` | Command deadline when Harbor supplies none |
| `HARBOR_KUBEVIRT_TRANSFER_TIMEOUT` | `300` | Per-transfer deadline, seconds |
| `HARBOR_WINDOWS_AGENT_COMMAND` | required for default bridge | Prepared Windows command/entrypoint |
| `HARBOR_WINDOWS_AGENT_CHECK_COMMAND` | empty | Optional agent readiness command |
| `HARBOR_WINDOWS_AGENT_VERSION` | unknown | Version recorded in Harbor results |

Harbor's own environment/agent/verifier deadlines still apply. Configure the
external tasks' startup timeout to allow Windows boot and image cloning.
Generic port variables take precedence over the legacy WAA names. Explicitly
empty generic port values select the defaults (5000/automatic), preserving
caller overrides. Services are named `<vm>-waa` or `<vm>-ale` for the selected
protocol and cleaned up with the VM's existing ownership checks.
`OPIK_URL` selects the existing `opik harbor` wrapper when nonempty; an empty
value selects Harbor directly. The command bridge does not emit ATIF trajectories
or agent-specific realtime tracing hooks.

## Harbor and agent contracts

The consuming project declares `[environment].os = "windows"`. An optional
Windows absolute `workdir` is created on startup; otherwise commands run in
`C:/workspace`. The task's `cpus` and `memory_mb` configure VM CPU cores (one
socket) and memory in MiB; omitted values use 2 vCPUs and 4 GiB. CDI derives the
clone's disk capacity from the golden PVC; `storage_mb` is rejected. Create
defines a stopped VM and its DataVolume template, then the backend requests
power-on. It supports CPU/memory limit policies, not Kubernetes request
or guarantee policies.

Select this environment through Harbor's supported import path:

```text
--env kubevirt_windows.environment:KubeVirtWindowsEnvironment
```

Add `Agents/utils/common/Harbor` to the controller's `PYTHONPATH` when invoking
Harbor outside the provided launcher. The backend implements start/stop,
execution, file/directory transfer, and filtered artifact downloads. Harbor's
normal Windows `.bat` entrypoints and result handling remain in use.

The default `WindowsCommandAgent` calls an image-provided or runtime-prepared
command, without invoking Harbor's Linux-oriented `BaseInstalledAgent`. Its contract is:

- Read the UTF-8 task instruction from the file named by
  `HARBOR_INSTRUCTION_FILE`. The instruction is never interpolated into a shell
  command.
- Read `HARBOR_MODEL` if model selection is needed. Configure provider settings
  using the external project's agent environment settings; the runner does not
  copy its entire environment or kubeconfig into Windows.
- Run synchronously and return a nonzero exit code on failure. Full stdout and
  stderr are collected in `agent/stdout.txt` and `agent/stderr.txt`.
- Agent setup may use the preparation manifest below. MCP/skills injection,
  token accounting, and ATIF conversion are not implemented by this generic bridge.

For a prepared Claude Code image, an example command is
`call claude -p --model "%HARBOR_MODEL%" < "%HARBOR_INSTRUCTION_FILE%"`, paired
with Harbor's `--model` option and the required agent credentials. This does not
use Agent Fleet's Linux Claude installer. Other prepared agents can expose the
same file/environment contract through their own entrypoint.

Use `--agent your_module:WindowsAgent` to select another Harbor agent with
`SUPPORTS_WINDOWS = True`. `--agent oracle` is available for reference solutions
provided by the other project. Windows environment support does not make the
existing Claude Code/OpenCode/Pi Linux adapters Windows-compatible.

## Runtime tool preparation

Keep Windows, drivers, Office/browser installations, and reboot-requiring
prerequisites in a versioned base image. Put frequently changing agent binaries,
portable dependencies, and bootstrap scripts in an immutable runner-side cache.
Each fresh VM receives the selected cached files; the controller does not download
packages or run a public installer for every trial.

```text
versioned Windows/app image → fresh VM → verified cached tools → readiness check
                                                               ↓
                                      task instruction + runtime credentials
                                                               ↓
                                                    agent → artifacts → delete
```

The optional `HARBOR_WINDOWS_PREPARE_MANIFEST` points to a local JSON file. It can
also be passed as the agent argument `--ak prepare_manifest=/path/prepare.json`;
an explicit empty argument disables the environment setting. Existing runs that
omit it continue to use their preinstalled agent.

Example manifest (replace each all-zero digest with the cached file's SHA256):

```json
{
  "version": 1,
  "files": [
    {
      "source": "cache/agent-tools-v1.zip",
      "target": "C:/agent-tools/agent-tools-v1.zip",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    },
    {
      "source": "cache/prepare-v1.ps1",
      "target": "C:/agent-tools/prepare.ps1",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  ],
  "commands": [
    "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:/agent-tools/prepare.ps1"
  ],
  "env": {
    "AGENT_TOOLS_ROOT": "C:/agent-tools/v1"
  }
}
```

The consuming project supplies the versioned bundle and script; for example, a
bundle can contain its selected Codex version and prerequisites. The script owns
unpacking, tool configuration, and any required checks. Keep privileged or
reboot-requiring setup in the base image. This is not a package manager or a
Codex-specific installer.

- `source` is a regular local file; relative paths resolve beside the manifest.
  Maintain cache files as immutable while trials use them. All file checksums are
  checked before the first upload. `target` must be an absolute Windows path.
- `commands` run in order with `cmd.exe` semantics. Invoke PowerShell explicitly.
  A nonzero status fails agent setup before readiness or task execution. Commands
  should be idempotent; successful preparation is not cached inside the guest.
- `env` contains non-secret string values used for preparation, the agent readiness
  check, and agent execution. Harbor's scoped agent environment overrides these
  defaults. Values are literal; use full executable paths or a
  wrapper script to extend `PATH`. Environment changes inside a preparation process
  do not persist into later guest commands or the verifier.
- Use Harbor's agent environment configuration for credentials at runtime. Do not
  place secrets in images, cached bundles, scripts, or the manifest. The manifest
  is trusted operator input and is not a task-controlled download instruction.
- `--ak prepare_timeout_sec=600` bounds checksum verification, uploads, and all
  preparation commands together (default 600 seconds). Harbor's agent setup
  deadline also applies; configure it to cover provisioning. The readiness check
  follows preparation and has its own 60-second deadline.
- Agent result metadata records `preparation_sha256`, the digest of the manifest
  bytes, without recording its environment values. Keep the manifest and pinned
  artifacts externally for reproducibility. No prepare/checkpoint/fan-out cache
  or shared writable tool volume is created by this backend.

```bash
export HARBOR_WINDOWS_PREPARE_MANIFEST=/srv/windows-tools/prepare.json
export HARBOR_WINDOWS_AGENT_COMMAND='C:\agent-tools\v1\run-agent.cmd'
export HARBOR_WINDOWS_AGENT_CHECK_COMMAND='C:\agent-tools\v1\run-agent.cmd --version'
./Agents/utils/common/Harbor/run_kubevirt_windows.sh \
  --path /data/external-windows-tasks --n-concurrent 1 \
  --ak prepare_timeout_sec=600
```

## Benchmark scope and reset boundary

This backend integrates WAA and ALE Windows command/file services for VM lifecycle,
agent preparation, command execution, and artifact collection. It does not add a
benchmark adapter, task setup/reset logic, evaluators, scoring, or GUI tools.
The image already includes desktop-control endpoints, but exposing screenshot,
input, or accessibility APIs to agents is outside this transport's contract.
The consuming benchmark project owns those integrations and desktop validation.

Reset is a **fresh persistent disk clone per trial**. Restarting a retained VM is
not a reset. Benchmark application-state snapshot restoration, tool-disk attachment,
and prepared-snapshot caching are outside this backend. Keep base image versions,
tool manifests, and task assets independently versioned.

## Execution, isolation, and cleanup

Guest commands use **cmd.exe semantics**, matching Harbor's Windows helpers.
Call PowerShell explicitly for `.ps1` scripts. Commands, cwd, and environment are
uploaded in a JSON file through the selected guest service; shell command length does not constrain
task instructions or environment values. The PowerShell supervisor bounds the
command's lifetime and requests process-tree termination on timeout/cancellation.
WAA caps an `/execute` request at 120 seconds. The backend launches its
PowerShell supervisor as a detached process, then polls an atomically published
result using short `/execute` requests. ALE launches the same supervisor and
polls through `/cmd` with `run_command`. Agent commands still run synchronously
inside the supervisor. If HTTP access is lost, cancellation is best effort;
VM teardown remains the cleanup boundary. Agent-created detached/background
processes are not a supported agent model.

Each `exec()` returns at most the last 1 MiB of each output stream, with a
truncation marker. Full per-command output stays under
`C:/ProgramData/AgentFleet/<vm>/` until deletion; download it explicitly if needed.
The command bridge redirects agent output to Harbor's collected logs.
Output callbacks fire when a command completes, not continuously.

Guest HTTP connects to `node_ip:node_port` from a NodePort `Service` that maps
`HARBOR_KUBEVIRT_GUEST_PORT` onto a cluster node IP the runner can reach; there is
no port-forward, SSH, or virtctl proxy. Both image-local services provide unauthenticated
command execution and file access over HTTP. Use a trusted private guest network
with access restricted to the runner and operators; do not publish port 5000 to
untrusted clients. This backend does not configure those network restrictions or
add guest authentication. mTLS apiserver credentials are sent only to the
kube-apiserver, never to the guest; guest requests ignore controller HTTP proxy
environment variables.

Migration from earlier Windows backend revisions: replace the custom
platform HTTP API with the KubeVirt-native control plane, remove
`HARBOR_KUBEVIRT_BASE_URL`/`TOKEN`/`SUBNET` platform settings and
`HARBOR_KUBEVIRT_SSH_*` settings, and use `HARBOR_KUBEVIRT_KUBECONFIG` (+ optional
`HARBOR_KUBEVIRT_API_SERVER`) with `HARBOR_KUBEVIRT_IMAGE` as a golden PVC name.
`HARBOR_KUBEVIRT_STORAGE_CLASS` now takes a Kubernetes storage class name, not
a platform storage-class ID. Allow runner-to-node port access for the NodePort
guest Service.

Migration from the shared-hostDisk revision requires a one-time image import.
With the source Windows VM shut down, upload its clean disk using the operator's
[CDI image upload tooling](https://kubevirt.io/user-guide/storage/containerized_data_importer/),
for example (choose a capacity large enough for your image and a valid storage class):

```bash
virtctl image-upload pvc windows-golden-v1 --namespace default \
  --size=64Gi --storage-class=windows-storage --image-path=/srv/windows-clean.raw
export HARBOR_KUBEVIRT_IMAGE=windows-golden-v1
export HARBOR_KUBEVIRT_STORAGE_CLASS=windows-storage
```

This operator import is separate from workload startup. The launcher still uses
only the apiserver. A per-trial DataVolume is recorded in `kubevirt.json` along
with the source PVC; no host disk is attached directly. There is no SSH/WinRM
fallback. The Harbor environment import path is unchanged.

Startup failures and cancellation attempt VM cleanup. Normal `stop(delete=True)`
reads the VM, checks the trial ownership label, then stops and deletes it and
cleans up the selected guest NodePort Service. Services carry a VM owner reference so
Kubernetes also garbage-collects them. Explicit service cleanup checks the trial
label and uses a UID deletion precondition. If VM deletion succeeds but service
cleanup fails, subsequent cleanup retries still check and delete the service;
retry state is cleared only after cleanup succeeds. Missing or mismatched VM
labels block VM mutation; mismatched service labels block service deletion.
VM ownership checks remain read-before-delete and do not lock against concurrent
replacement. Harbor's retention mode (`delete=False`) halts the VM and retains
its cloned disk and Service for inspection.
Retained instances are not reused for subsequent trials. VM identity is saved
in each trial's `kubevirt.json`; use it for operator cleanup after a controller
crash or failed API request. There is no automatic orphan reaper in this version.

Only public/default network policy is supported. Restricted network policies,
GPU/TPU requests, Docker/Compose definitions, arbitrary host mounts, and user
impersonation fail explicitly. Directory downloads exclude Windows reparse
points; uploads reject symlinks. Desktop screenshot/input control is outside
this transport; consumers may integrate the selected guest's desktop endpoints.

## Local validation

```bash
PYTHONPATH=.:Agents/utils/common/Harbor python3 -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_kubevirt*.py' -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/kubevirt_windows \
  Agents/utils/common/Harbor/tests/test_kubevirt*.py
bash -n Agents/utils/common/Harbor/run_kubevirt_windows.sh
```

Tests use the real pinned Harbor interfaces, synthetic cluster settings, mocked platform/guest responses,
and a loopback HTTP server for file-transfer requests. They require no kubeconfig
or cluster credentials and do not boot Windows.
Before declaring an image usable, run a single trial against a fresh clone and
verify: guest readiness after boot, Unicode/binary upload/download, an agent command
lasting more than 120 seconds, timeout/process-tree cancellation, artifact
collection, and ownership-checked VM deletion. The existing-guest checks below
cover preparation and supervisor behavior; they do not replace fresh-clone
validation on the target platform and image.

## Live guest preparation validation (2026-09-28)

Revision `aa2703a30dd51958ce5562fdd23a528cef1f41f9` passed **40 live checks** on
existing WAA instances 07–10 (`julyai/mywinarena:v1`). The controller used Python
3.12.13, Harbor 0.18.0, and httpx 0.28.1; guest commands ran as the WAA `Docker`
account under Windows PowerShell 5.1.26100.1591. A separate run of **22 focused
preparation and WAA transport tests** also passed; platform/lifecycle tests were
excluded from this validation session.

The live harness used the unmodified guest transport and preparation methods,
attaching directly to the existing WAA endpoints without constructing a platform
client or invoking VM lifecycle methods. Each guest passed:

- Checksum-pinned local installation of Codex CLI 0.155.1 into a fresh test
  directory, repeat setup, and native executable `--version` readiness.
- Checksum rejection before any upload, installation failure skipping readiness,
  and readiness failure propagation.
- A 1 MiB binary roundtrip through a Unicode path, Unicode stdout/stderr,
  nonzero exit propagation, and final WAA/agent health and request-file cleanup.

Instance 07 completed a 125-second preparation command followed by readiness,
exceeding WAA's per-request limit. Instances 08 and 09 verified child-process
termination after a command timeout and an overall preparation deadline,
respectively. Instance 10 verified bounded stdout/stderr tails and truncation
markers.

These were fresh installation directories on existing guests, not fresh image
clones. The installer and bundle were supplied as validation fixtures; this does
not add a built-in Codex installer. No credentials, model requests, agent task
runs, GUI automation, or benchmark scoring were tested. KubeVirt provisioning,
boot, resource configuration, retention/deletion, and compatibility of the
published WAA-V2 snapshot remain outside these results. No implementation fixes
were required by these checks.

## Historical hostDisk validation (2026-09-29)

These results predate the per-trial CDI cloning fix. They establish transport
and guest behavior only; they do not validate the current clone lifecycle.
Before production use, validate concurrent clone isolation, sequential clean
state, retention, and VM/DataVolume/PVC/Service garbage collection on the target
cluster.

This revision moved the control plane to KubeVirt's native apiserver API. Live
checks on a three-node KubeVirt cluster (cpu-nat-131 control-plane,
cpu-nat-184, cpu-nat-391) confirmed:

- **mTLS to the apiserver** from the runner via client certificate/key decoded
  from the kubeconfig (no `kubectl`/`virtctl` invoked).
- **VM lifecycle** against the real cluster: `create` (a `VirtualMachine`
  reusing an existing `hostDisk`, e.g. `minimal.raw`, on cpu-nat-391),
  `start`, read of the pod IP from the VMI `status.interfaces`, and `delete`.
  The control plane returned correct phase/ready/IP values; a VM launched and
  the launcher pod was scheduled on the image's node.
- **Runner-to-guest reachability.** The runner has no route into the pod
  overlay (10.90.x.x). A **NodePort `Service`** mapping the WAA port onto a
  node IP (`node_ip:node_port`) bridged this: `GET /probe` returned
  `Service is operational`, and a full guest `execute` (including the
  PowerShell supervisor, upload of `execute.ps1`, and output collection)
  returned the expected session account (`win-...\docker`).

Important operational findings:

- **Root disk bus.** Windows golden images lack a virtio storage driver; a
  virtio root disk BSODs with `INACCESSIBLE_BOOT_DEVICE (0x7B)`. The backend
  attaches the root disk on **SATA** (`DEFAULT_DISK_BUS`), overridable with
  `HARBOR_KUBEVIRT_DISK_BUS`.
- **hostDisk is shared in place.** A `hostDisk` reuses the same raw file on the
  node; starting a second VM that points at a file already attached by a
  running VM (here each of the four golden disks was in use by a live Win
  guest) causes the launcher to crash-loop (`CrashLoopBackOff`). A fresh trial
  VM therefore needs a disk that is not currently attached to another VM, or a
  node-local copy.
- The running Win guests were not disturbed by these checks; the control-plane
  and NodePort/transport paths were validated against them read-only or via a
  throwaway Service that was deleted afterwards.

## Historical hostDisk end-to-end task run (2026-09-29)

The `minimal-waa` image (a purpose-built Windows 11 + WAA command-server image)
was deployed to cpu-nat-184 as `minimal-waa.raw` and used for real Harbor trials
against the KubeVirt-native backend:

- **Oracle agent** — three small Windows tasks (create a fixed text file, report
  the logged-in user, compute an arithmetic result) each **passed (reward 1.0)**.
  This validated the full path: VM create/start via the apiserver, NodePort WAA
  exposure, in-guest `solve.bat` execution, `test.bat` writing
  `C:/logs/verifier/reward.txt`, download, and reward parsing.
- **Real LLM agent** — the same tasks were solved by the `WindowsCommandAgent`
  bridge with an in-guest runner that read `HARBOR_INSTRUCTION_FILE`, called an
  OpenAI-compatible/anthropic-messages LLM endpoint for a Windows command, and
  executed it via WAA. All three tasks **passed (reward 1.0)**, with the verifier
  confirming the guest-side artifacts the LLM produced.

Authoring a task for this backend requires an (empty) `environment/` directory so
Harbor classifies the path as a task rather than a dataset, and a `.bat` verifier
that writes `C:/logs/verifier/reward.txt` (beware Windows `echo 1>` being parsed
as a stdout FD redirect — use `> path echo 1`).
