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

## Host and image requirements

- Prepare the pinned Harbor runner with repository setup. Workload startup only
  validates it; it does not install tools or download agent runtimes.
- Confirm network reachability to the platform HTTP API at
  `HARBOR_KUBEVIRT_BASE_URL` and to the guest WAA service on TCP port 5000.
  No SSH/SFTP, WinRM, `kubectl`, `virtctl`, or kubeconfig is used.
- Provide a platform access token via `HARBOR_KUBEVIRT_TOKEN` with permission to
  create/get/stop/delete VMs and read available IPs in the configured namespace.
- Import the [WAA-V2 Windows 11 snapshot](https://huggingface.co/datasets/henryhe0123/WAA-V2-win11-snapshot)
  through the platform's image workflow, then set `HARBOR_KUBEVIRT_IMAGE` to its
  versioned platform image name. Image download, import/conversion, and driver
  repair are operator steps; this backend does not perform them.
- Preserve the image's WAA service and logged-in session startup. WAA's
  [setup script](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/setup.ps1#L380-L429)
  opens port 5000 and registers the server at logon. The backend waits for
  `/probe`, then uses `/execute`, `/setup/upload`, and `/file` from the
  [guest server](https://github.com/GAIR-NLP/WindowsAgentArena-V2/blob/2927fe55005d1be75d5a9188c0045f73ba28d192/src/win-arena-container/vm/setup/server/main.py).
  A running VM without a running WAA service is insufficient.
- The imported image must boot with the platform's disk/network devices and
  VirtIO drivers. Windows PowerShell 5.1+ and the WAA session account must be
  able to create `C:/ProgramData/AgentFleet`, `C:/logs`, and the workspace.
  Commands run as the WAA server's account; user selection and automatic
  privilege escalation are unsupported. Agent tools can be supplied by the
  runtime preparation manifest below.

> **Validation boundary.** The WAA API contract is source-checked at the pinned
> revision above and exercised by local HTTP tests. No imported WAA snapshot,
> Windows PowerShell process, or complete WAA benchmark run has been validated
> on KubeVirt by this change. ALE, OSWorld, arbitrary Windows images, and other
> guest protocols are not supported targets of this PR.

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
export HARBOR_KUBEVIRT_IMAGE=waa-v2-win11-v1
export HARBOR_KUBEVIRT_NAMESPACE=windows-benchmarks
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
| `HARBOR_KUBEVIRT_WAA_PORT` | `5000` | Guest WAA HTTP port |
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

Guest HTTP connects directly to the VM's assigned IP and
`HARBOR_KUBEVIRT_WAA_PORT`; there is no port-forward or SSH bootstrap. WAA's
service provides unauthenticated command execution and file access over HTTP.
Use a trusted private guest network with access restricted to the runner and
operators; do not publish port 5000 to untrusted clients. This backend does not
configure those network restrictions or add guest authentication. Platform
credentials are sent only to the platform API, never to WAA; guest requests
ignore controller HTTP proxy environment variables.

Migration from earlier revisions of this unmerged PR: replace the generic
Windows/SSH template with an imported WAA image, remove `HARBOR_KUBEVIRT_SSH_*`
settings, and allow runner-to-guest TCP 5000 (or the configured WAA port).
There is no SSH/WinRM fallback. The Harbor environment import path is unchanged.

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
collection, and ownership-checked VM deletion. Image compatibility, guest
PowerShell behavior, and actual Windows agent execution remain live checks.
