#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"

set +e
"$SCRIPT_DIR/harboropik.sh"
status="$?"
set -e

show_registry_summary() {
  echo
  if [[ -f "$OUTPUT_PATH/summary.txt" ]]; then
    cat "$OUTPUT_PATH/summary.txt"
  else
    echo "summary unavailable: $OUTPUT_PATH/summary.txt"
  fi
}

# A zero process status is complete only when the summary writer found the
# aggregate Harbor result. Keep incomplete and failed panes available so the
# error that preceded the summary cannot disappear behind "Bye from Zellij!".
if [[ "$status" -eq 0 ]] &&
   ! grep -qx 'status:      complete' "$OUTPUT_PATH/summary.txt" 2>/dev/null; then
  status=1
  printf '%s\n' "$status" > "$HARBOR_BENCHMARK_EXIT_FILE"
fi

if [[ "$status" -ne 0 ]]; then
  show_registry_summary
  if [[ "${HARBOR_ZELLIJ_KEEP_ON_FAILURE:-1}" == "1" ]]; then
    echo
    echo "Harbor failed; keeping this pane open for diagnostics."
    echo "Press Ctrl-q to leave Zellij after reviewing the error above."
    while true; do
      sleep 3600
    done
  fi
  exit "$status"
fi

if [[ "$HARBOR_ZELLIJ_CLOSE_ON_COMPLETE" != "1" ]]; then
  show_registry_summary
  echo "HARBOR_ZELLIJ_CLOSE_ON_COMPLETE=0; keeping final registry pane open"
  while true; do
    sleep 3600
  done
fi

exit "$status"
