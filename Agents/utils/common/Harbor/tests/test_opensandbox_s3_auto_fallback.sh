#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Isolate the helper so this contract test does not source the full runner.
eval "$(sed -n '/^harbor_prewarm_s3_upload_sources()/,/^}/p' "$HARBOR_DIR/env/dependencies.sh")"
eval "$(sed -n '/^harbor_prewarm_s3_upload_cache()/,/^}/p' "$HARBOR_DIR/env/dependencies.sh")"

mkdir -p "$tmp/deps"
HARBOR_ENVIRONMENT_TYPE=opensandbox
YICLOUD_SANDBOX_UPLOAD_BACKEND=auto
LOCAL_WHEEL_DIR="$tmp/deps"
AGENT=claude-code
HARBOR_CC_CLAUDE_TGZ_SOURCE="$tmp/missing-claude.tgz"
HARBOR_CC_HOOK_SOURCE="$tmp/missing-hook.py"
HARBOR_CC_WEB_MCP_SOURCE=""
SCRIPT_DIR="$HARBOR_DIR"
verifier_runtime_bundle_required() { return 1; }

# Simulate an unavailable S3 prewarm without touching external state.
python3() { return 1; }

output="$(harbor_prewarm_s3_upload_cache 2>&1)"
grep -Fx -- 'prewarming immutable OpenSandbox S3 objects...' <<< "$output" >/dev/null
grep -Fx -- '[WARN] OpenSandbox S3 attempt failed backend=auto phase=prewarm; runtime will retry S3 and warn before any HTTP fallback' <<< "$output" >/dev/null

echo 'OpenSandbox S3 auto fallback warning test passed.'
