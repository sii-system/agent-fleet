#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
exec "${SELF_DIR}/python3" "${SELF_DIR}/self_check.py" "$@"
