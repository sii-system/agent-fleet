#!/usr/bin/env bash
set -euo pipefail

RL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
worker="$RL_DIR/run_rl_rollout_worker.sh"

bash -n "$worker"

idle_block="$(sed -n '/^idle_wait()/,/^}/p' "$worker")"
queue_block="$(sed -n '/^while true; do/,/request_id=/p' "$worker")"

[[ "$idle_block" == *'[[ -t 0 ]]'* ]]
[[ "$idle_block" == *'read -r -t'* ]]
[[ "$queue_block" == *'if claim_request; then'* ]]
[[ "$queue_block" == *'request_file="$CLAIMED_REQUEST"'* ]]
[[ "$queue_block" == *'idle_wait'* ]]
[[ "$queue_block" != *'sleep '* ]]
[[ "$queue_block" != *'$(claim_request'* ]]
[[ "$queue_block" != *'claim_request || true'* ]]

echo "rollout worker idle wait test passed"
