#!/usr/bin/env bash
set -euo pipefail

mkdir -p /logs/verifier /logs/artifacts
printf '0\n' > /logs/verifier/reward.txt
printf 'NATIVE-E2B\n' | cmp - /app/answer.txt
cp /app/answer.txt /logs/artifacts/e2b-smoke.txt
printf '1\n' > /logs/verifier/reward.txt
