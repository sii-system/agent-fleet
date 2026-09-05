#!/usr/bin/env bash
set -euo pipefail

# Harbor's execution verifier only indicates that the harness produced an
# answer. BrowseComp correctness is judged host-side after collection; no gold
# answer or qrels are copied into this container.
mkdir -p /logs/verifier
if find /logs/agent -type f -size +0c -print -quit 2>/dev/null | grep -q .; then
  printf '1\n' > /logs/verifier/reward.txt
else
  printf '0\n' > /logs/verifier/reward.txt
fi
