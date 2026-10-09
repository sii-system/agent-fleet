# WindowsAgentArena Harbor adapters

| Taskset / `--benchmark` | Pinned source | Full manifest |
| --- | --- | --- |
| `waa` | [Microsoft WAA at `6d39ed88`](https://github.com/microsoft/WindowsAgentArena/tree/6d39ed88c545a0d40a7a02e39b928e278df7332b) | 154 tasks / 12 domains |
| `waa-v2` (`waa2` fleet alias) | [WAA-V2 at `2927fe55`](https://github.com/GAIR-NLP/WindowsAgentArena-V2/tree/2927fe55005d1be75d5a9188c0045f73ba28d192) | 141 tasks / 12 domains |

The adapter generates Windows Harbor tasks with instructions, unchanged native
JSON, and release/domain/ID provenance. `WAAEnvironment` performs native setup on
a fresh KubeVirt VM; `WAAAgent` executes PC-Agent or a custom predict/reset agent;
`WAAVerifier` returns native rewards through Harbor's verifier interface. Harbor
owns scheduling, phase timeouts, retries, resume, and result reporting.

PC-Agent comes from the pinned V2 client and can drive either release. On original
WAA it is an alternative to Microsoft's Navi baseline. Native task setup, getters,
postconfig, metric composition, and infeasible-task scoring remain unchanged.
The standard `examples` and `test_all.json` are used; V2's pinned release does not
include the `examples_noctxt` directory mentioned by upstream.

## Prepare

Install Git, `uv`, and a C/C++ compiler for `fastdtw`, then prepare the
Python 3.12 environment and both upstream clients explicitly:

```bash
./Tasks/WindowsAgentArena/setup.sh
# Optional clean checkout at the exact pinned revision:
./Tasks/WindowsAgentArena/setup.sh --benchmark waa --source /data/WindowsAgentArena
./Tasks/WindowsAgentArena/setup.sh --benchmark waa-v2 --source /data/WindowsAgentArena-V2
```

Setup validates all native setup/getter/metric imports without VM or model traffic.
Dependencies are declared in `pyproject.toml`; `uv sync` creates a local, ignored
`uv.lock`. Setup records its hash and installed versions for startup validation.
Fresh setups can resolve different transitive dependency versions.
The cache defaults to `${AGENT_FLEET_CACHE_DIR:-~/.cache/agent-fleet}/waa`; override
`HARBOR_WAA_CACHE_DIR` and optionally `HARBOR_WAA_ENV_DIR`. Startup validates the
prepared source/dependencies and never installs them. Keep editable environments
separate between worktrees. Generated task datasets remain outside the repository.

Prepare **a separate application-complete Windows image for each release** using
its upstream instructions, including profiles/data, matching WAA server, and an
automatically logged-in desktop. Import its shut-down disk into an immutable PVC
following the [KubeVirt contract](../../Agents/utils/common/Harbor/KUBEVIRT_WINDOWS_README.md).
A command server alone is insufficient. Publish a new PVC name when an image changes.
The host needs mTLS kube-apiserver access and reachable node NodePorts. Guest services
must expose 5000 (command server), 9222 (browser debugging), and 8080 (VLC HTTP).
The ports belong to the trial's owned Service and are removed with its VM. Enable
these services in the image and restrict NodePort access to the runner's network.

Keep private settings in `config.local.env` or the environment:

```bash
export HARBOR_KUBEVIRT_KUBECONFIG=$HOME/.kube/config
export HARBOR_KUBEVIRT_NAMESPACE=windows-benchmarks
export HARBOR_KUBEVIRT_IMAGE=waa-v2-windows11-golden-v1
export BASE_URL=https://api.example.com/v1
export API_KEY=sk-fake
export MODEL=your-vision-model
```

PC-Agent uses OpenAI-compatible vision chat completions. `BASE_URL` / `API_KEY`
provide `OPENAI_BASE_URL` / `OPENAI_API_KEY` defaults; explicit OpenAI values,
including empty values, take precedence. Secrets are inherited, not stored in tasks.

## Run through Harbor

```bash
./Tasks/WindowsAgentArena/run.sh --benchmark waa --all --dry-run
./Tasks/WindowsAgentArena/run.sh --benchmark waa-v2 --all --workers 4 \
  --output /data/runs/waa-v2-full
./Tasks/WindowsAgentArena/run.sh --benchmark waa-v2 --task DOMAIN/TASK_ID \
  --output /data/runs/waa-canary -- --ak max_steps=30 --max-retries 2

HARBOR_KUBEVIRT_IMAGE=waa-windows11-golden-v1 \
  ./scripts/run_fleet.sh --taskset waa --agent pcagent --workers 4
./scripts/run_fleet.sh --taskset waa-v2 --agent pcagent --workers 4
```

The dedicated launcher defaults to V2. `--all`, repeated `--domain`, and
repeated/comma-separated `--task` are mutually exclusive. Full-manifest validation
precedes filtering. Harbor task names are `DOMAIN--TASK_ID`. Pass additional
**native Harbor options after `--`**, including agent kwargs (`--ak max_steps=30`,
`--ak pause=0`), environment kwargs (`--ek action_space=code_block`, `--ek a11y=true`),
resource overrides, and timeout/retry options. Custom agents use `--agent module:factory`;
see the [agent interface](../../Agents/WindowsAgentArena/README.md).

V2 defaults to 1280x720 observations; override with `--ek 'screen_size=[1920,1080]'`.
Original WAA preserves actual screenshot dimensions because its actions use desktop
pixels. Factories receive the actual observation size. Native stabilization waits
remain intact, and exhausting the prediction budget still reaches verification.

`--output` or `OUTPUT_PATH` selects a Harbor job directory. Otherwise Harbor creates
a job under `${OUTPUT_ROOT:-<repo>/runs}`; FleetSpec's `RUN_ID` becomes its job name.
FleetSpec/prompt mode support both tasksets. Unified `--output` saves FleetSpec JSON.
Runs stay in the foreground; unified `--detach` warns. With `OPIK_URL` set, the
launcher executes the shared prepared `opik harbor` CLI (`HARBOR_OPIK_BIN` overrides
it); initialize the Opik submodule and run `./scripts/setup.sh` first. Native client
phases still use the prepared WAA environment. The zellij monitor is not started.

Use Harbor's own resume command and error filters:

```bash
WAA_ENV=$HOME/.cache/agent-fleet/waa/venv
PYTHONDONTWRITEBYTECODE=1 HARBOR_KUBEVIRT_GUEST_PROTOCOL=waa HARBOR_KUBEVIRT_EXTRA_PORTS=9222,8080 \
  PYTHONPATH="$PWD:$PWD/Agents/utils/common/Harbor:$PWD/Tasks/WindowsAgentArena/src" \
  "$WAA_ENV/bin/harbor" jobs resume --job-path /data/runs/waa-v2-full
# Add --filter-error-type RuntimeError, etc., to retry selected failures.
```

Restore the same cluster/image/model settings for resume. After a hard kill, inspect
recorded `kubevirt.json` resources and perform ownership-checked cleanup before retrying;
Harbor may remove failed trial directories when filtering errors. Graceful trial
cancellation kills the native worker before VM cleanup. Never delete the golden PVC.

Harbor writes job `config.json`, `lock.json`, `result.json`, and per-trial results and
`verifier/reward.txt`. `kubevirt.json`, `waa-worker.json`, and `waa-worker.log` identify
the VM/client session. Native artifacts live in `agent/native/`: WAA uses
`traj.jsonl`/`traj.html`; V2 uses `traj.jsonl`/`traj.md`/`results.json`; both retain
`result.txt`. Zero and negative finite native rewards are preserved. Read Harbor's
reward and exception results together to distinguish scores from infrastructure errors.
Before stopping the VM, the shared Windows backend snapshots `C:/logs/agent`,
`C:/logs/verifier`, and `C:/logs/artifacts` so Harbor can recover guest logs even
after a native setup failure has already deleted the VM. Collection respects
Harbor's log filters. Snapshot failures or the configured transfer timeout do
not prevent VM cleanup; host-native trajectories remain in `agent/native/`.

## Materialize for direct Harbor use

```bash
export PYTHONPATH="$PWD:$PWD/Agents/utils/common/Harbor:$PWD/Tasks/WindowsAgentArena/src"
export PYTHONDONTWRITEBYTECODE=1 HARBOR_KUBEVIRT_GUEST_PROTOCOL=waa HARBOR_KUBEVIRT_EXTRA_PORTS=9222,8080
WAA_ENV=$HOME/.cache/agent-fleet/waa/venv
WAA_RUNTIME=$HOME/.cache/agent-fleet/waa/runtime/6d39ed88c545a0d40a7a02e39b928e278df7332b
WAA_PC_RUNTIME=$HOME/.cache/agent-fleet/waa/runtime/2927fe55005d1be75d5a9188c0045f73ba28d192
"$WAA_ENV/bin/python" -m waa_benchmark.adapter --benchmark waa --runtime "$WAA_RUNTIME" --output-dir /data/harbor/waa
"$WAA_ENV/bin/harbor" run --path /data/harbor/waa \
  --agent Agents.WindowsAgentArena.agent:WAAAgent --model your-vision-model \
  --env waa_benchmark.environment:WAAEnvironment --ek "runtime=$WAA_RUNTIME" --ek "pcagent_runtime=$WAA_PC_RUNTIME" \
  --verifier waa_benchmark.verifier:WAAVerifier --n-concurrent 4
```

Direct Harbor runs require the same cluster and `OPENAI_*` settings. For V2 use
`--benchmark waa-v2` and `WAA_PC_RUNTIME` for both runtime paths. The custom verifier
is required: fallback `test.bat` fails without a reward when it is omitted. Native
verification uses the agent's shared VM. Prepared clients change import loading
(lazy metric/getter exports and deferred unused OCR); KubeVirt replaces QEMU lifecycle
and captures screenshots through guest HTTP. Identify the backend and image version
when reporting benchmark scores.

## Checks

```bash
PYTHONPATH=. python3 -m unittest discover -s Tasks/WindowsAgentArena/tests -v
WAA_TEST_RUNTIME="$WAA_PC_RUNTIME" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  "$WAA_ENV/bin/python" -m unittest discover -s Tasks/WindowsAgentArena/tests -v
```

CI checks both full manifests and runs actual Harbor trials against pinned clients
with loopback command/model services. These tests do not establish live Windows
image readiness. Run a canary on each image before the full benchmark.
