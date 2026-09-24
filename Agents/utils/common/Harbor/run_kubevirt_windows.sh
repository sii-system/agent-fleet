#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
# shellcheck source=../../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

if [[ "${1:-}" == "--help" || "$#" == 0 ]]; then
  cat <<'HELP'
Usage: run_kubevirt_windows.sh [--dry-run] <Harbor run arguments>

Runs externally supplied Windows Harbor tasks on isolated KubeVirt VMs.
Example: run_kubevirt_windows.sh --path /data/windows-tasks --n-concurrent 1
         run_kubevirt_windows.sh --path /data/windows-tasks --agent oracle

Required: HARBOR_KUBEVIRT_BASE_URL, HARBOR_KUBEVIRT_TOKEN,
          HARBOR_KUBEVIRT_IMAGE, HARBOR_KUBEVIRT_NAMESPACE,
          HARBOR_KUBEVIRT_SSH_USER, HARBOR_KUBEVIRT_SSH_KEY.
The default agent also requires HARBOR_WINDOWS_AGENT_COMMAND.
See KUBEVIRT_WINDOWS_README.md for the VM and agent contracts.
HELP
  exit 0
fi

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=1
  shift
fi
HARBOR_RUNNER_IMAGE_DIR="${HARBOR_RUNNER_IMAGE_DIR:-/opt/harbor-runner}"
HARBOR_RUNNER_HOST_DIR="${HARBOR_RUNNER_HOST_DIR:-$HOME/.local/share/agent-fleet/harbor-runner}"
if [[ -z "${HARBOR_RUNNER_DIR:-}" ]]; then
  if [[ -d "$HARBOR_RUNNER_IMAGE_DIR" ]]; then
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_IMAGE_DIR"
  else
    HARBOR_RUNNER_DIR="$HARBOR_RUNNER_HOST_DIR"
  fi
fi
export HARBOR_CLI_BIN="${HARBOR_CLI_BIN:-$HARBOR_RUNNER_DIR/bin/harbor}"
export HARBOR_OPIK_BIN="${HARBOR_OPIK_BIN:-$HARBOR_RUNNER_DIR/bin/opik}"
export HARBOR_OPIK_PYTHON="${HARBOR_OPIK_PYTHON:-$HARBOR_RUNNER_DIR/bin/python}"
export HARBOR_RUNNER_REQUIREMENTS="${HARBOR_RUNNER_REQUIREMENTS:-$SCRIPT_DIR/runner-requirements.txt}"
if [[ -z "${HARBOR_RUNNER_PYTHON_VERSION:-}" && -f "$HARBOR_RUNNER_DIR/.agent-fleet-python-version" ]]; then
  IFS= read -r HARBOR_RUNNER_PYTHON_VERSION < "$HARBOR_RUNNER_DIR/.agent-fleet-python-version"
fi
export HARBOR_RUNNER_PYTHON_VERSION="${HARBOR_RUNNER_PYTHON_VERSION:-3.12.13}"
export HARBOR_ENVIRONMENT_TYPE=kubevirt-windows
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

cmd=("$HARBOR_CLI_BIN")
if agent_fleet_opik_enabled; then
  cmd=("$HARBOR_OPIK_BIN" harbor)
fi
cmd+=(run --agent kubevirt_windows.agent:WindowsCommandAgent "$@"
  --env kubevirt_windows.environment:KubeVirtWindowsEnvironment)
if [[ "$dry_run" == 1 ]]; then
  # Do not render arguments: --ae and --ak can contain caller secrets.
  echo '[DRY-RUN] Harbor Windows run; backend=kubevirt_windows.environment:KubeVirtWindowsEnvironment'
  echo '[DRY-RUN] No cluster access, runner validation, or guest changes performed.'
  exit 0
fi
if [[ ! -x "$HARBOR_OPIK_PYTHON" ]]; then
  echo '[ERROR] Harbor runner is missing; run ./scripts/setup.sh first.' >&2
  exit 1
fi
"$HARBOR_OPIK_PYTHON" "$SCRIPT_DIR/harbor_prepare_runner_cli.py" --validate
"$HARBOR_OPIK_PYTHON" -c 'from kubevirt_windows.environment import KubeVirtWindowsEnvironment; KubeVirtWindowsEnvironment.preflight()'
exec "${cmd[@]}"
