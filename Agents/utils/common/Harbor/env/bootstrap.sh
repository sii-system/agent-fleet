#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Load shared site configuration with the same precedence used by the top-level
# CLI and DinD entry point: runtime/exported values, config.local.env,
# config.env, then the defaults below.
# shellcheck source=../../../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"
# shellcheck source=../opensandbox_s3_profile.sh
source "$SCRIPT_DIR/opensandbox_s3_profile.sh"

# Keep setup-managed and explicitly supplied prerequisite directories visible
# for direct Harbor entry points as well as scripts/run_fleet.sh.
# shellcheck source=../../../../../scripts/prerequisites.sh
source "$REPO_ROOT/scripts/prerequisites.sh"
agent_fleet_prerequisite_init_path
agent_fleet_prerequisite_init_runtime
