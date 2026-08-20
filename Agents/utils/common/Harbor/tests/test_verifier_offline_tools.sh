#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHIM="$HARBOR_DIR/verifier-tools/curl"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p "$tmp/offline" "$tmp/home"
cat >"$tmp/offline/uv" <<'SH'
#!/usr/bin/env sh
printf 'offline uv\n'
SH
cat >"$tmp/offline/uvx" <<'SH'
#!/usr/bin/env sh
printf 'offline uvx\n'
SH
chmod +x "$tmp/offline/uv" "$tmp/offline/uvx"

installer="$("$SHIM" -LsSf https://astral.sh/uv/0.7.13/install.sh)"
HOME="$tmp/home" HARBOR_VERIFIER_UV_BIN_DIR="$tmp/offline" sh -c "$installer"

[[ -x "$tmp/home/.local/bin/uv" ]]
[[ -x "$tmp/home/.local/bin/uvx" ]]
[[ "$("$tmp/home/.local/bin/uv")" == "offline uv" ]]
[[ "$("$tmp/home/.local/bin/uvx")" == "offline uvx" ]]
grep -Fx 'export PATH="$HOME/.local/bin:$PATH"' "$tmp/home/.local/bin/env" >/dev/null

# Non-uv curl behavior must remain available to benchmark scripts.
"$SHIM" --version | grep -F 'curl ' >/dev/null
