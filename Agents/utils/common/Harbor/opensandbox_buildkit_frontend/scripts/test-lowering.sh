#!/usr/bin/env bash
set -euo pipefail

: "${FRONTEND_WORK_DIR:?set the completed frontend build directory}"
component=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mapfile -t upstream < "$component/upstream.version"
export GOTOOLCHAIN=go1.25.4 CGO_ENABLED=0
export GOPATH="$FRONTEND_WORK_DIR/go" GOCACHE="$FRONTEND_WORK_DIR/go-cache"
export OPENSANDBOX_CORPUS="$FRONTEND_WORK_DIR/corpus.json"
python3 "$component/tests/corpus.py" > "$OPENSANDBOX_CORPUS"
git -C "$FRONTEND_WORK_DIR/upstream" worktree add --detach "$FRONTEND_WORK_DIR/stock" "${upstream[1]}"
for mode in stock upstream; do
    destination="$FRONTEND_WORK_DIR/$mode/frontend/dockerfile/dockerfile2llb"
    cp "$component/tests/lowering_test.go" "$destination/opensandbox_test.go"
    export OPENSANDBOX_PATCHED=0
    if [[ "$mode" == upstream ]]; then
        cp "$component/tests/cache_test.go" "$destination/opensandbox_cache_test.go"
        export OPENSANDBOX_PATCHED=1
    fi
    export OPENSANDBOX_SNAPSHOTS="$FRONTEND_WORK_DIR/$mode-snapshots"
    mkdir -p "$OPENSANDBOX_SNAPSHOTS"
    (
        cd "$FRONTEND_WORK_DIR/$mode"
        timeout 180 go test -buildvcs=false -mod=vendor -tags=dfrunsecurity,dfrundevice \
            ./frontend/dockerfile/dockerfile2llb -run TestOpenSandbox -v
    ) > "$FRONTEND_WORK_DIR/$mode-lowering.log" 2>&1
done
python3 "$component/tests/compare_lowering.py" \
    "$FRONTEND_WORK_DIR/stock-snapshots" "$FRONTEND_WORK_DIR/upstream-snapshots"
git -C "$FRONTEND_WORK_DIR/upstream" worktree remove --force "$FRONTEND_WORK_DIR/stock"
