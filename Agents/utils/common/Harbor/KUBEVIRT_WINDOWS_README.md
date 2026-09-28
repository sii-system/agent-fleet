# Windows environments on KubeVirt

This backend implements Harbor 0.18.0's `BaseEnvironment` using an isolated
Windows VM per trial. The Harbor controller runs on Linux. Benchmark tasks,
datasets, setup logic, evaluators, and scoring belong to the consuming project.
No benchmark adapter is included here.

Use `run_kubevirt_windows.sh` for Windows runs. It uses the existing config
loader and pinned runner validation, but avoids the Linux dependency installers,
bind mounts, and agent wrappers used by `start.sh` / `run_fleet.sh`. Those unified
launchers do not yet dispatch Windows runs. The backend is also importable
directly by another project's Harbor configuration.

## Host and image requirements

- Prepare the pinned Harbor runner with repository setup. Workload startup only
  validates it; it does not install tools or download agent runtimes.
- Install OpenSSH `ssh` / `sftp` on the Linux runner and confirm network
  reachability to the platform HTTP API at `HARBOR_KUBEVIRT_BASE_URL`. No
  `kubectl`, `virtctl`, or kubeconfig is required.
- Provide a platform access token via `HARBOR_KUBEVIRT_TOKEN` with permission to
  create/get/stop/delete VMs and read available IPs in the configured namespace.
- The platform clones the selected golden image (`HARBOR_KUBEVIRT_IMAGE`) into a
  fresh VM per trial, so no operator-owned VM manifest, DataVolume, or portable
  disk clone is needed. The backend generates VM names and labels.
- Prepare a versioned Windows image with VirtIO drivers, Windows PowerShell 5.1
  or later, OpenSSH Server with SFTP and key authentication, and the desired
  stable applications already installed. Agent tools may be delivered at runtime
  using the preparation manifest below. The SSH account must be able to create
  `C:/ProgramData/AgentFleet`, `C:/logs`, and the task workspace. Commands execute
  as that account; alternate users and automatic privilege escalation are not
  supported. Each trial gets a freshly cloned disk; restarting an existing VM is
  not the reset mechanism.

> **Validation boundary.** Earlier validation of this PR exercised the platform
> lifecycle with a Linux template. It did not validate a Windows image or Windows
> command execution. The runner could open TCP to the guest IP, but SSH stalled
> during banner exchange; the platform did not inject a usable SSH key. VNC console
> access does not supply this backend's execution/file-transfer transport. A
> Windows image, reachable SSH/SFTP, and a provisioned guest key remain required.
> These observations describe the earlier test environment, not a current image
> catalog or a guarantee of platform readiness.

The platform owns the golden-image clone and VM lifecycle: each trial creates a
fresh VM from `HARBOR_KUBEVIRT_IMAGE`, and teardown requests release the cloned
storage. The backend does not delete the golden source image or the guest
credential Secret the platform creates at VM creation.

## Configuration and launch

Put private settings in ignored `config.local.env` or exported environment
variables. Runtime environment values, including explicitly empty ones, override
saved configuration.

```bash
export HARBOR_KUBEVIRT_BASE_URL=https://vm-platform.example.com
# export HARBOR_KUBEVIRT_TOKEN=replace-with-a-platform-access-token
export HARBOR_KUBEVIRT_IMAGE=windows-benchmark-v1
export HARBOR_KUBEVIRT_NAMESPACE=windows-benchmarks
export HARBOR_KUBEVIRT_SSH_USER=runner
export HARBOR_KUBEVIRT_SSH_KEY=/path/to/runner-key
export HARBOR_WINDOWS_AGENT_COMMAND='C:\Agent\run-agent.cmd'

./Agents/utils/common/Harbor/run_kubevirt_windows.sh --dry-run \
  --path /data/external-windows-tasks --n-concurrent 1

./Agents/utils/common/Harbor/run_kubevirt_windows.sh \
  --path /data/external-windows-tasks --n-concurrent 1
```

Dry-run makes no platform API calls and deliberately does not print raw
arguments, which may include secrets. It does not validate the image or the
platform token.

Optional settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HARBOR_KUBEVIRT_BASE_URL` | required | Platform HTTP API base URL (no trailing `/api/v1`) |
| `HARBOR_KUBEVIRT_TOKEN` | required | Platform access token |
| `HARBOR_KUBEVIRT_IMAGE` | required | Golden image name to clone per trial |
| `HARBOR_KUBEVIRT_SUBNET` | `ovn-default` | Platform subnet for the VM IP |
| `HARBOR_KUBEVIRT_STORAGE_CLASS` | `ceph-rbd-sc` | Platform storage class for the root disk |
| `HARBOR_KUBEVIRT_SSH_PORT` | `22` | Guest SSH port |
| `HARBOR_KUBEVIRT_START_TIMEOUT` | `1800` | Total create/clone/boot/guest-readiness deadline, seconds. Template-image provisioning alone can take ~10 min; keep this generous |
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
  do not persist into later SSH commands or the verifier.
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

## Desktop and snapshot integration boundary

This PR provides VM lifecycle, command execution, file transfer, and runtime tool
preparation. It does not establish an interactive desktop session. A desktop
benchmark integration must separately prepare and validate its logged-in GUI
session, virtual display/resolution/DPI, screenshot/input/UIA service, application
state, task setup, and evaluator. An SSH process is not evidence that GUI actions
run in the intended desktop session.

The reset boundary remains **fresh template clone per trial**. Restarting a
retained VM is not a reset. Importing benchmark disks, restoring application-state
snapshots, attaching tool disks, and building a prepared-snapshot cache require
provider APIs and image workflows outside this PR. Keep base image versions,
tool manifests, and per-task assets independently versioned; benchmark-specific
adapters and scoring remain in the consuming project.

## Execution, isolation, and cleanup

Guest commands use **cmd.exe semantics**, matching Harbor's Windows helpers.
Call PowerShell explicitly for `.ps1` scripts. Commands, cwd, and environment are
transferred in a JSON request over SFTP; shell command length does not constrain
task instructions or environment values. The PowerShell supervisor bounds the
command's lifetime and requests process-tree termination on timeout/cancellation.
If SSH is lost, remote cancellation is best effort; VM teardown remains the
cleanup boundary. Detached/background processes are not a supported agent model.

Each `exec()` returns at most the last 1 MiB of each output stream, with a
truncation marker. Full per-command output stays under
`C:/ProgramData/AgentFleet/<vm>/` until deletion; download it explicitly if needed.
The command bridge redirects agent output to Harbor's collected logs.
Output callbacks fire when a command completes, not continuously.

SSH and SFTP connect directly to the VM's assigned IP address on the configured
`HARBOR_KUBEVIRT_SSH_PORT` (no `virtctl` port-forward). The first guest host key
is accepted through that direct connection and pinned in a private per-instance
`known_hosts` file; changed keys are rejected. Verify that the runner can route
to the VM subnet.

Startup failures and cancellation attempt VM cleanup. Normal `stop(delete=True)`
reads the VM and checks the trial ownership label before stop/delete requests.
Missing or mismatched labels block both mutations; a missing VM is treated as
already cleaned up. The HTTP API does not provide an atomic UID precondition,
so this read-before-delete check is not a lock against concurrent replacement. Harbor's
retention mode (`delete=False`) halts the VM and retains its disks for inspection.
Retained instances are not reused for subsequent trials. VM identity is saved
in each trial's `kubevirt.json`; use it for operator cleanup after a controller
crash or failed API request. There is no automatic orphan reaper in this version.

Only public/default network policy is supported. Restricted network policies,
GPU/TPU requests, Docker/Compose definitions, arbitrary host mounts, and user
impersonation fail explicitly. Directory downloads exclude Windows reparse
points; uploads reject symlinks. Desktop screenshot/input control is outside
this transport and requires additional guest tools.

## Local validation

```bash
PYTHONPATH=.:Agents/utils/common/Harbor python3 -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_kubevirt*.py' -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/kubevirt_windows \
  Agents/utils/common/Harbor/tests/test_kubevirt*.py
bash -n Agents/utils/common/Harbor/run_kubevirt_windows.sh
```

Tests use the real pinned Harbor interfaces with a mocked HTTP transport and SSH
operations. They do not boot Windows. Guest PowerShell behavior, image
compatibility, platform RBAC, and actual Windows agent execution require a
later live validation.
