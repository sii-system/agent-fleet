"""Load repository configuration before executing a Harbor Python CLI."""

import os
import sys
from pathlib import Path


def exec_with_repository_config(entrypoint: Path) -> None:
    entrypoint = entrypoint.resolve()
    repo_root = entrypoint.parents[5]
    if os.environ.get("AGENT_FLEET_CONFIG_LOADED_ROOT") == str(repo_root):
        return
    loader = repo_root / "scripts" / "config_loader.sh"
    command = (
        'set -euo pipefail; source "$1"; agent_fleet_load_config "$2"; '
        'python_bin="$3"; shift 3; exec "$python_bin" "$@"'
    )
    os.execvp(
        "bash",
        [
            "bash",
            "-c",
            command,
            "harbor-config",
            str(loader),
            str(repo_root),
            sys.executable,
            str(entrypoint),
            *sys.argv[1:],
        ],
    )
