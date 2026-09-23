#!/usr/bin/env bash
set -euo pipefail

RL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARBOR_SCRIPT_DIR="${HARBOR_SCRIPT_DIR:-$(cd "$RL_SCRIPT_DIR/../common/Harbor" && pwd)}"
. "$HARBOR_SCRIPT_DIR/env.sh"

if [[ "$HARBOR_NATIVE_CONCURRENCY" == "1" ]]; then
  RL_WORKERS=1
fi

OUT="${1:-$LAYOUT_FILE}"
mkdir -p "$(dirname "$OUT")"

if [[ "${RL_ZELLIJ_ROLE:-}" != "job" ]]; then
  echo "RL rollout listener is not a zellij layout; use run_rl_rollout_server.sh for port ${RL_PORT}." >&2
  echo "Set RL_ZELLIJ_ROLE=job only when generating per-submission worker layouts." >&2
  exit 2
fi

emit_worker_pane() {
  local worker_id="$1"
  cat >> "$OUT" <<EOF
        pane {
          command "$RL_SCRIPT_DIR/run_rl_rollout_worker.sh"
          args "$worker_id"
        }
EOF
}

job_label="${RL_ZELLIJ_SUBMISSION_ID:-submission}"
if [[ "${#job_label}" -gt 18 ]]; then
  job_label="${job_label: -18}"
fi

cat > "$OUT" <<EOF
layout {
  default_tab_template {
    pane size=1 borderless=true {
      plugin location="zellij:tab-bar"
    }
    children
    pane size=2 borderless=true {
      plugin location="zellij:status-bar"
    }
  }

  tab name="job-${job_label}" focus=true {
    pane split_direction="vertical" {
      pane size="50%" {
        command "$RL_SCRIPT_DIR/monitor_rl_rollout.sh"
      }
      pane size="50%" split_direction="horizontal" {
EOF

# Match the normal agent-fleet layout: keep overview readable and spill extra
# workers into additional tabs instead of squeezing every worker beside monitor.
overview_end=5
if [[ "$RL_WORKERS" -lt "$overview_end" ]]; then
  overview_end="$RL_WORKERS"
fi

if [[ "$overview_end" -gt 0 ]]; then
  for i in $(seq 1 "$overview_end"); do
    emit_worker_pane "$i"
  done
fi

cat >> "$OUT" <<'EOF'
      }
    }
  }
EOF

start=6
while [[ "$start" -le "$RL_WORKERS" ]]; do
  end=$((start + 9))
  if [[ "$end" -gt "$RL_WORKERS" ]]; then
    end="$RL_WORKERS"
  fi

  cat >> "$OUT" <<EOF
  tab name="workers-${start}-${end}" {
    pane split_direction="vertical" {
      pane size="50%" split_direction="horizontal" {
EOF

  left_end=$((start + 4))
  if [[ "$left_end" -gt "$end" ]]; then
    left_end="$end"
  fi
  for i in $(seq "$start" "$left_end"); do
    emit_worker_pane "$i"
  done

  cat >> "$OUT" <<'EOF'
      }
      pane size="50%" split_direction="horizontal" {
EOF

  right_start=$((start + 5))
  if [[ "$right_start" -le "$end" ]]; then
    for i in $(seq "$right_start" "$end"); do
      emit_worker_pane "$i"
    done
  fi

  cat >> "$OUT" <<'EOF'
      }
    }
  }
EOF

  start=$((end + 1))
done

cat >> "$OUT" <<'EOF'
}
EOF

echo "Wrote RL job layout to $OUT"
