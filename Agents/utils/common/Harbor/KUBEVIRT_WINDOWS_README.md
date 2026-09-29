# WAA Windows images on KubeVirt

This backend implements Harbor 0.18.0's `BaseEnvironment` using an isolated
WindowsAgentArena (WAA) VM per trial. The first target is the published
WAA-V2 Windows 11 snapshot. The Harbor controller runs on Linux. Benchmark tasks,
datasets, setup logic, evaluators, and scoring belong to the consuming project.
No benchmark adapter is included here.

Use `run_kubevirt_windows.sh` for Windows runs. It uses the existing config
loader and pinned runner validation, but avoids the Linux dependency installers,
bind mounts, and agent wrappers used by `start.sh` / `run_fleet.sh`. Those unified
launchers do not yet dispatch Windows runs. The backend is also importable
directly by another project's Harbor configuration.

## KubeVirt-native VM lifecycle

The control plane follows KubeVirt's own API surface and talks **only** to the
kube-apiserver over HTTPS; it never shells out to `kubectl`, `virtctl`, or a
client library. Each trial is a fresh `VirtualMachine` (`kubevirt.io/v1`) that
reuses an existing **`hostDisk`** golden image on one of the cluster's nodes
(e.g. `/var/lib/kubevirt/custom-disks/minimal.raw`). Because the golden disk is
reused in place, a cloned VM must run on the node that holds that file
(`HARBOR_KUBEVIRT_NODE`); use a DataVolume-backed template if you need replicas
across restarts and concurrent trials from one image.

- Create: `POST /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines`
- Read status + pod IP: `GET .../virtualmachineinstances/{name}` (`status.interfaces[].ipAddress`)
- Start/stop: `PUT /apis/subresources.kubevirt.io/v1/.../virtualmachines/{name}/start|stop`
- Delete: `DELETE /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}`

The VM spec matches a proven Windows guest: SATA root disk (the golden image
carries no virtio storage driver), masquerade pod network with the WAA port
declared, EFI secure boot, and cloud-init. The runner lives outside the pod
overlay, so the backend additionally creates a **NodePort `Service`** that maps
the WAA port onto a node IP the runner can reach, and connects the guest
transport to `node_ip:node_port`.

## Host and image requirements

- Prepare the pinned Harbor runner with repository setup; workload startup only
  validates it and does not install Windows tools.
- Confirm the runner can reach the kube-apiserver (`HARBOR_KUBEVIRT_API_SERVER`
  or the kubeconfig's `server`) with mTLS, and can reach node ports on a cluster
  node (the NodePort Service exposes WAA there). No SSH/SFTP, WinRM, or client
  tools are used.
- Provide a kubeconfig (`HARBOR_KUBEVIRT_KUBECONFIG`, default `~/.kube/config`)
  whose current user presents client certificate + key with permission to
  create/get/start/stop/delete `virtualmachines` and `services` in the namespace.
  Alternatively set `HARBOR_KUBEVIRT_API_SERVER` (the kubeconfig is still read
  for the mTLS credentials). Token auth is not supported.
- Place a bootable Windows image on a node as a raw file, then set
  `HARBOR_KUBEVIRT_IMAGE` to its absolute `hostDisk` path (e.g.
  `/var/lib/kubevirt/custom-disks/minimal.raw`) and `HARBOR_KUBEVIRT_NODE` to
  the node holding that file. Note that a `hostDisk` is shared in place: start a
  clean clone of the image and keep concurrent trials on different images.
- Preserve the image's WAA service and logged-in session startup. WAA's
  [setup script](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/setup.ps1#L380-L429)
  opens port 5000 and registers the server at logon. The backend waits for
  `/probe`, then uses `/execute`, `/setup/upload`, and `/file` from the
  [guest server](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/server/main.py).
  A running VM without a running WAA service is insufficient.
- The image must boot with the node's disk/network devices and VirtIO drivers.
  Windows PowerShell 5.1+ and the WAA session account must be able to create
  `C:/ProgramData/AgentFleet`, `C:/logs`, and the workspace. Commands run as the
  WAA server's account; user selection and automatic privilege escalation are
  unsupported. Agent tools can be supplied by the runtime preparation manifest
  below.

> **Validation boundary.** The WAA API contract is source-checked at the pinned
> revision above and exercised by local HTTP tests. Guest preparation and the
> PowerShell supervisor were validated on existing Windows guests (see live
> validation below). This revision replaces the custom platform HTTP API with a
> KubeVirt-native control plane, live-validated for VM create/start/read/delete,
> NodePort WAA exposure, and guest execution. Complete Harbor benchmark runs and
> production image management remain the consuming project's responsibility.
> ALE, OSWorld, arbitrary Windows images, and other guest protocols are not
> supported targets of this PR.

## Configuration and launch

Put private settings in ignored `config.local.env` or exported environment
variables. Runtime environment values, including explicitly empty ones, override
saved configuration.

```bash
export HARBOR_KUBEVIRT_KUBECONFIG=$HOME/.kube/config
export HARBOR_KUBEVIRT_API_SERVER=https://10.254.64.34:6443
export HARBOR_KUBEVIRT_IMAGE=/var/lib/kubevirt/custom-disks/minimal.raw
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
| `HARBOR_KUBEVIRT_IMAGE` | required | Absolute `hostDisk` path to the golden image on the node |
| `HARBOR_KUBEVIRT_NODE` | empty | Node (hostname) that holds the `hostDisk` image; required for a hostDisk reusing the existing file |
| `HARBOR_KUBEVIRT_NAMESPACE` | `default` | Namespace for the VM and WAA Service |
| `HARBOR_KUBEVIRT_DISK_BUS` | `sata` | Root disk bus (sata/virtio/scsi); Windows golden images need SATA |
| `HARBOR_KUBEVIRT_WAA_PORT` | `5000` | Guest WAA HTTP port |
| `HARBOR_KUBEVIRT_WAA_NODE_PORT` | auto | Optional explicit nodePort for the WAA Service |
| `HARBOR_KUBEVIRT_START_TIMEOUT` | `1800` | Total create/boot/guest-readiness deadline, seconds. Windows boot can take ~10 min; keep this generous |
| `HARBOR_KUBEVIRT_COMMAND_TIMEOUT` | `3600` | Command deadline when Harbor supplies none |
| `HARBOR_KUBEVIRT_TRANSFER_TIMEOUT` | `300` | Per-transfer deadline, seconds |
| `HARBOR_WINDOWS_AGENT_COMMAND` | required for default bridge | Prepared Windows command/entrypoint |
| `HARBOR_WINDOWS_AGENT_CHECK_COMMAND` | empty | Optional agent readiness command |
| `HARBOR_WINDOWS_AGENT_VERSION` | unknown | Version recorded in Harbor results |

Harbor's own environment/agent/verifier deadlines still apply. Configure the
external tasks' startup timeout to allow Windows boot and image cloning.
`OPIK_URL` selects the existing `opik harbor` wrapper when nonempty; an empty
value selects Harbor directly. The command bridge does not emit ATIF trajectories
or agent-specific realtime tracing hooks.

## Harbor and agent contracts

The consuming project declares `[environment].os = "windows"`. An optional
Windows absolute `workdir` is created on startup; otherwise commands run in
`C:/workspace`. The task's `cpus` and `memory_mb` become `cpuCores` (one socket) and
`memoryGuest` in MiB in the create request; omitted values use 2 vCPUs and 4 GiB.
Disk size follows the template minimum; `storage_mb` is rejected. The backend sizes the root disk to at least the source template's
`minSize` (a template-image clone cannot be smaller than its source), then
sends an explicit power-on after create (create defines the VM in a Stopped
state). It supports CPU/memory limit policies, not Kubernetes request
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

## WAA scope and reset boundary

This PR integrates WAA's existing command/file service for VM lifecycle, agent
preparation, command execution, and artifact collection. It does not add a WAA
benchmark adapter, task setup/reset logic, evaluators, scoring, or GUI tools.
The image already includes desktop-control endpoints, but exposing screenshot,
input, or accessibility APIs to agents is outside this transport's contract.
The consuming benchmark project owns those integrations and desktop validation.

Reset remains **fresh template clone per trial**. Restarting a retained VM is
not a reset. WAA application-state snapshot restoration, tool-disk attachment,
and prepared-snapshot caching are outside this PR. Keep base image versions,
tool manifests, and task assets independently versioned.

## Execution, isolation, and cleanup

Guest commands use **cmd.exe semantics**, matching Harbor's Windows helpers.
Call PowerShell explicitly for `.ps1` scripts. Commands, cwd, and environment are
uploaded in a JSON file through WAA; shell command length does not constrain
task instructions or environment values. The PowerShell supervisor bounds the
command's lifetime and requests process-tree termination on timeout/cancellation.
WAA caps an `/execute` request at 120 seconds. The backend launches its
PowerShell supervisor as a detached process, then polls an atomically published
result using short `/execute` requests. Agent commands still run synchronously
inside the supervisor. If HTTP access is lost, cancellation is best effort;
VM teardown remains the cleanup boundary. Agent-created detached/background
processes are not a supported agent model.

Each `exec()` returns at most the last 1 MiB of each output stream, with a
truncation marker. Full per-command output stays under
`C:/ProgramData/AgentFleet/<vm>/` until deletion; download it explicitly if needed.
The command bridge redirects agent output to Harbor's collected logs.
Output callbacks fire when a command completes, not continuously.

Guest HTTP connects to `node_ip:node_port` from a NodePort `Service` that maps
`HARBOR_KUBEVIRT_WAA_PORT` onto a cluster node IP the runner can reach; there is
no port-forward, SSH, or virtctl proxy. WAA's service provides unauthenticated
command execution and file access over HTTP. Use a trusted private guest network
with access restricted to the runner and operators; do not publish port 5000 to
untrusted clients. This backend does not configure those network restrictions or
add guest authentication. mTLS apiserver credentials are sent only to the
kube-apiserver, never to WAA; guest requests ignore controller HTTP proxy
environment variables.

Migration from earlier revisions of this unmerged PR: replace the custom
platform HTTP API with the KubeVirt-native control plane, remove
`HARBOR_KUBEVIRT_BASE_URL`/`TOKEN`/`SUBNET`/`STORAGE_CLASS` platform settings and
`HARBOR_KUBEVIRT_SSH_*` settings, and use `HARBOR_KUBEVIRT_KUBECONFIG` (+ optional
`HARBOR_KUBEVIRT_API_SERVER`) with `HARBOR_KUBEVIRT_IMAGE` as a hostDisk path and
`HARBOR_KUBEVIRT_NODE`. Allow runner-to-node port access for the NodePort WAA
Service. There is no SSH/WinRM fallback. The Harbor environment import path is
unchanged.

Startup failures and cancellation attempt VM cleanup. Normal `stop(delete=True)`
reads the VM, checks the trial ownership label, then stops the VM, deletes the VM
object, and deletes the WAA NodePort Service. Missing or mismatched labels block
both mutations; a missing VM is treated as already cleaned up. The apiserver does
not provide an atomic UID precondition, so this read-before-delete check is not a
lock against concurrent replacement. Harbor's retention mode (`delete=False`)
halts the VM and retains its disks for inspection.
Retained instances are not reused for subsequent trials. VM identity is saved
in each trial's `kubevirt.json`; use it for operator cleanup after a controller
crash or failed API request. There is no automatic orphan reaper in this version.

Only public/default network policy is supported. Restricted network policies,
GPU/TPU requests, Docker/Compose definitions, arbitrary host mounts, and user
impersonation fail explicitly. Directory downloads exclude Windows reparse
points; uploads reject symlinks. Desktop screenshot/input control is outside
this transport; consumers may integrate WAA's existing desktop endpoints.

## Local validation

```bash
PYTHONPATH=.:Agents/utils/common/Harbor python3 -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_kubevirt*.py' -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/kubevirt_windows \
  Agents/utils/common/Harbor/tests/test_kubevirt*.py
bash -n Agents/utils/common/Harbor/run_kubevirt_windows.sh
```

Tests use the real pinned Harbor interfaces, mocked platform/guest responses,
and a loopback HTTP server for file-transfer requests. They do not boot Windows.
Before declaring an image usable, run a single trial against a fresh clone and
verify: WAA readiness after boot, Unicode/binary upload/download, an agent command
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

## Live KubeVirt-native validation (2026-09-29)

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

## Live end-to-end task run (2026-09-29)

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
