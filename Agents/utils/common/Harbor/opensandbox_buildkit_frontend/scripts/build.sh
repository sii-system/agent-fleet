#!/usr/bin/env bash
set -euo pipefail

# Build only a local OCI layout. All downloads go through the Gateway.
: "${ARTIFACT_CACHE_GATEWAY_URL:?set the platform Gateway /v1/cache URL}"
: "${FRONTEND_WORK_DIR:?set a fresh /data build directory}"
component=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mapfile -t upstream < "$component/upstream.version"
# Setup owns Go installation; cold builds only select a validated compiler.
source "$component/../../../../../scripts/prerequisites.sh"
go_binary=$(agent_fleet_find_frontend_go)
test ! -e "$FRONTEND_WORK_DIR"
mkdir -p "$FRONTEND_WORK_DIR/image"
export GOPROXY="${ARTIFACT_CACHE_GATEWAY_URL}/go-proxy"
export GOSUMDB="sum.golang.org ${ARTIFACT_CACHE_GATEWAY_URL}/go-sumdb"
export GOTOOLCHAIN=local
unset GOROOT
export GOCACHE="$FRONTEND_WORK_DIR/go-cache"
export GOPATH="$FRONTEND_WORK_DIR/go"
export CGO_ENABLED=0
gateway_host=$(python3 -c 'import sys; from urllib.parse import urlsplit; print(urlsplit(sys.argv[1]).hostname)' "$ARTIFACT_CACHE_GATEWAY_URL")
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$gateway_host"
export no_proxy="${no_proxy:+$no_proxy,}$gateway_host"
timeout 600 git clone --single-branch --branch "${upstream[0]}" \
    "${ARTIFACT_CACHE_GATEWAY_URL%/v1/cache}/v1/git/github/moby/buildkit.git" \
    "$FRONTEND_WORK_DIR/upstream"
test "$(git -C "$FRONTEND_WORK_DIR/upstream" rev-parse HEAD)" = "${upstream[1]}"
git -C "$FRONTEND_WORK_DIR/upstream" apply --check "$component/patches/0001-opensandbox-run-instrumentation.patch"
git -C "$FRONTEND_WORK_DIR/upstream" apply "$component/patches/0001-opensandbox-run-instrumentation.patch"
cp "$component/instrumentation.go" "$FRONTEND_WORK_DIR/upstream/frontend/dockerfile/dockerfile2llb/opensandbox.go"
(
    cd "$FRONTEND_WORK_DIR/upstream"
    timeout 600 "$go_binary" build -buildvcs=false -mod=vendor -trimpath \
        -tags=dfrunsecurity,dfrundevice \
        -o "$FRONTEND_WORK_DIR/image/dockerfile-frontend" \
        ./frontend/dockerfile/cmd/dockerfile-frontend
)
cp "$component/Dockerfile" "$component/.dockerignore" "$FRONTEND_WORK_DIR/image/"
source_epoch=$(git -C "$FRONTEND_WORK_DIR/upstream" show -s --format=%ct "${upstream[1]}")
touch -d "@$source_epoch" "$FRONTEND_WORK_DIR/image/dockerfile-frontend"
timeout 180 docker buildx build --no-cache --network=none --provenance=false \
    --output "type=oci,dest=$FRONTEND_WORK_DIR/layout,tar=false,rewrite-timestamp=true" \
    --build-arg "SOURCE_DATE_EPOCH=$source_epoch" \
    --progress=plain "$FRONTEND_WORK_DIR/image"
