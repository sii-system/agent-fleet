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
  applications/agent already installed. The SSH account must be able to create
  `C:/ProgramData/AgentFleet`, `C:/logs`, and the task workspace. Commands execute
  as that account; alternate users and automatic privilege escalation are not
  supported. Each trial gets a freshly cloned disk; restarting an existing VM is
  not the reset mechanism.

> **Platform image availability.** The platform template catalog currently
> contains only Linux images (for example `ubuntu20.04-template-image`). Windows
> guest readiness (`execute.ps1`) is therefore only confirmed once a Windows
> golden image is provisioned. Lifecycle and direct-IP SSH plumbing can be
> validated with a Linux image as a stand-in today, but the Windows-specific
> `execute.ps1` contract remains untested until a Windows image exists.
>
> **Direct-IP SSH caveat (live-validated).** Against this platform from the
> runner, SSH does **not** complete: the runner establishes the TCP connection
> to the VM's overlay IP but the handshake times out at banner exchange, and the
> platform injects no SSH key (there is no cloud-init/access-credential API; the
> guest agent reports Offline). The only interactive access path exposed by the
> platform is the VNC/WebSocket console (`vnc/ws`, standard RFB, reachable
> through the gateway), not SSH. Control-plane lifecycle is fully validated; the
> SSH `execute.ps1` execution transport still requires a runner with route/key
> to the VM overlay.

The platform owns the golden-image clone and VM lifecycle: each trial creates a
fresh VM from `HARBOR_KUBEVIRT_IMAGE`, and teardown requests release the cloned
storage. The backend does not delete the golden source image or the guest
credential Secret the platform creates at VM creation.

## Configuration and launch

Put private settings in ignored `config.local.env` or exported environment
variables. Runtime environment values, including explicitly empty ones, override
saved configuration.

```bash
export HARBOR_KUBEVIRT_BASE_URL=http://10.9.202.91:31600
# export HARBOR_KUBEVIRT_TOKEN=replace-with-a-platform-access-token
export HARBOR_KUBEVIRT_IMAGE=ubuntu20.04-template-image
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
`C:/workspace`. The task's `cpus` and `memory_mb` override the image template's
CPU and guest RAM. Disk size is configured in the template; `storage_mb` is
rejected. The backend sizes the root disk to at least the source template's
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

The default `WindowsCommandAgent` calls a **preinstalled** command, without
invoking Harbor's Linux-oriented `BaseInstalledAgent`. Its contract is:

- Read the UTF-8 task instruction from the file named by
  `HARBOR_INSTRUCTION_FILE`. The instruction is never interpolated into a shell
  command.
- Read `HARBOR_MODEL` if model selection is needed. Configure provider settings
  using the external project's agent environment settings; the runner does not
  copy its entire environment or kubeconfig into Windows.
- Run synchronously and return a nonzero exit code on failure. Full stdout and
  stderr are collected in `agent/stdout.txt` and `agent/stderr.txt`.
- Agent setup is image-owned. MCP/skills injection, token accounting, and ATIF
  conversion are not implemented by this generic bridge.

For a prepared Claude Code image, an example command is
`call claude -p --model "%HARBOR_MODEL%" < "%HARBOR_INSTRUCTION_FILE%"`, paired
with Harbor's `--model` option and the required agent credentials. This does not
use Agent Fleet's Linux Claude installer. Other prepared agents can expose the
same file/environment contract through their own entrypoint.

Use `--agent your_module:WindowsAgent` to select another Harbor agent with
`SUPPORTS_WINDOWS = True`. `--agent oracle` is available for reference solutions
provided by the other project. Windows environment support does not make the
existing Claude Code/OpenCode/Pi Linux adapters Windows-compatible.

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
checks the trial ownership label and requests foreground deletion. Harbor's
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
PYTHONPATH=. python3 -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p test_kubevirt_windows.py -v
ruff check --config .github/ruff.toml \
  Agents/utils/common/Harbor/kubevirt_windows \
  Agents/utils/common/Harbor/tests/test_kubevirt_windows.py
bash -n Agents/utils/common/Harbor/run_kubevirt_windows.sh
```

Tests use the real pinned Harbor interfaces with a mocked HTTP transport and SSH
operations. They do not boot Windows. Guest PowerShell behavior, image
compatibility, platform RBAC, and actual Windows agent execution require a
later live validation.
