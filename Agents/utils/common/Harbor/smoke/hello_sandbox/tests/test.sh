#!/bin/bash
set -euo pipefail

expected='hello from qz sandbox'
actual="$(cat /app/hello.txt 2>/dev/null || true)"
if [ "$actual" = "$expected" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo "expected: $expected; actual: $actual" >&2
  echo 0 > /logs/verifier/reward.txt
fi
