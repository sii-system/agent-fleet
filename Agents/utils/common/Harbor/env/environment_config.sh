#!/usr/bin/env bash
set -euo pipefail

# Effective backend selection, verifier bundle policy, and image settings.
# Sourced by ../env.sh; SCRIPT_DIR remains the Harbor directory.

# Harbor accepts either a built-in environment name or a module:Class import
# path. YiCloud uses its own OpenAPI SDK and OGW signing, so it is loaded as a
# provider adapter without patching the PyPI Harbor package. qz (SII Inspire)
# exposes an E2B-compatible control plane, so its adapter reuses Harbor's E2B
# environment with qz connection settings mapped onto the official e2b SDK.
HARBOR_ENVIRONMENT_TYPE="${HARBOR_ENVIRONMENT_TYPE:-$RL_ENVIRONMENT_TYPE}"
if [[ -z "${HARBOR_ENVIRONMENT_SPEC:-}" ]]; then
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    HARBOR_ENVIRONMENT_SPEC="yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" && -n "$HARBOR_E2B_PREBUILT_TEMPLATE" ]]; then
    HARBOR_ENVIRONMENT_SPEC="$HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC"
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    HARBOR_ENVIRONMENT_SPEC="qz_e2b_sandbox:QzSandboxEnvironment"
  else
    HARBOR_ENVIRONMENT_SPEC="$HARBOR_ENVIRONMENT_TYPE"
  fi
fi
# qz does not provide Notebook-host bind mounts. Install the agent runtime
# inside the Sandbox instead of coupling setup to a runner-local dependency
# service. npmmirror is the regional-stability default, not the only reachable
# source: NPM_CONFIG_REGISTRY and QZ_NODE_DIST_URL allow an explicit upstream
# or private source. HARBOR_CC_NODE_DIST_URL reaches the agent as CC_NODE_DIST_URL.
QZ_NODE_DIST_URL="${QZ_NODE_DIST_URL:-}"
HARBOR_CC_NODE_DIST_URL="${HARBOR_CC_NODE_DIST_URL:-}"
if [[ -z "$NPM_CONFIG_REGISTRY" ]]; then
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    NPM_CONFIG_REGISTRY="https://registry.npmmirror.com"
  else
    NPM_CONFIG_REGISTRY="https://registry.npmjs.org"
  fi
fi
if [[ "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
  if [[ -n "$QZ_NODE_DIST_URL" ]]; then
    HARBOR_CC_NODE_DIST_URL="$QZ_NODE_DIST_URL"
  elif [[ -z "$HARBOR_CC_NODE_DIST_URL" ]]; then
    HARBOR_CC_NODE_DIST_URL="https://registry.npmmirror.com/-/binary/node/v22.14.0/node-v22.14.0-linux-x64.tar.gz"
  fi
fi
export QZ_NODE_DIST_URL HARBOR_CC_NODE_DIST_URL
HARBOR_OPENSANDBOX_IMAGE_REF="${HARBOR_OPENSANDBOX_IMAGE_REF:-}"
HARBOR_OPENSANDBOX_BUNDLE_MANIFEST="${HARBOR_OPENSANDBOX_BUNDLE_MANIFEST:-}"
YICLOUD_HARBOR_HOST="${YICLOUD_HARBOR_HOST:-}"
# Harbor Project is an externally provisioned benchmark mapping. A task
# repository is derived per task by the manager; never use a fixed repository.
YICLOUD_HARBOR_PROJECT="${YICLOUD_HARBOR_PROJECT:-}"
YICLOUD_HARBOR_TLS_VERIFY="${YICLOUD_HARBOR_TLS_VERIFY:-0}"
HARBOR_OPENSANDBOX_BENCHMARK="${HARBOR_OPENSANDBOX_BENCHMARK:-$DATASET_NAME}"

# Keep benchmark-specific verifier runtime policy in this thin selector. The
# OpenSandbox provider treats the selected archive as an opaque filesystem
# bundle and does not know which runtimes it contains.
select_verifier_runtime_bundle() {
  if [[ "$HARBOR_ENVIRONMENT_TYPE" != "opensandbox" ]]; then
    printf '%s\n' "none"
    return 0
  fi
  case "$HARBOR_OPENSANDBOX_BENCHMARK" in
    agent-fleet-swe-rebench-v2)
      printf '%s\n' "agent-fleet-swe-rebench-v2-verifier-bundle"
      ;;
    *)
      printf '%s\n' "none"
      ;;
  esac
}

resolve_verifier_runtime_bundle() {
  VERIFIER_RUNTIME_BUNDLE_ID="$1"
  VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE=""
  VERIFIER_RUNTIME_BUNDLE_ARCHIVE_MOUNT_PATH=""
  VERIFIER_RUNTIME_BUNDLE_ROOT=""
  VERIFIER_RUNTIME_BUNDLE_PREPARER=""
  case "$VERIFIER_RUNTIME_BUNDLE_ID" in
    none)
      ;;
    agent-fleet-swe-rebench-v2-verifier-bundle)
      VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE="$HARBOR_CC_PY_WHEEL_DIR_SOURCE/$VERIFIER_RUNTIME_BUNDLE_ID.tar.gz"
      VERIFIER_RUNTIME_BUNDLE_ARCHIVE_MOUNT_PATH="$HARBOR_CC_PY_WHEEL_DIR_MOUNT_PATH/$VERIFIER_RUNTIME_BUNDLE_ID.tar.gz"
      VERIFIER_RUNTIME_BUNDLE_ROOT="/tmp/harbor-verifier-bundles/$VERIFIER_RUNTIME_BUNDLE_ID"
      VERIFIER_RUNTIME_BUNDLE_PREPARER="$SCRIPT_DIR/verifier_runtime/swe_rebench_v2_bundle_preparer.py"
      ;;
    *)
      echo "[ERROR] unknown verifier runtime bundle: $VERIFIER_RUNTIME_BUNDLE_ID" >&2
      return 1
      ;;
  esac
}

resolve_verifier_runtime_bundle "$(select_verifier_runtime_bundle)"

verifier_runtime_bundle_required() {
  [[ "$VERIFIER_RUNTIME_BUNDLE_ID" != "none" ]]
}

validate_verifier_runtime_bundle_transport() {
  verifier_runtime_bundle_required || return 0
  case "$YICLOUD_SANDBOX_UPLOAD_BACKEND" in
    s3|auto)
      ;;
    *)
      echo "[ERROR] verifier runtime bundle $VERIFIER_RUNTIME_BUNDLE_ID requires YICLOUD_SANDBOX_UPLOAD_BACKEND=s3 or auto" >&2
      return 1
      ;;
  esac
}

HARBOR_OPENSANDBOX_DOCKER_CONFIG="${HARBOR_OPENSANDBOX_DOCKER_CONFIG:-$HOME/.docker/config.json}"
HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT="${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT:-/data/harbor-runs/opensandbox-images}"
HARBOR_OPENSANDBOX_IMAGE_PLATFORM="${HARBOR_OPENSANDBOX_IMAGE_PLATFORM:-linux/amd64}"
HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX="${HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX:-harbor}"
HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX="${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX:-m.daocloud.io/docker.io}"
HARBOR_OPENSANDBOX_APT_MIRROR="${HARBOR_OPENSANDBOX_APT_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn}"
HARBOR_OPENSANDBOX_PIP_INDEX_URL="${HARBOR_OPENSANDBOX_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
HARBOR_OPENSANDBOX_NPM_REGISTRY="${HARBOR_OPENSANDBOX_NPM_REGISTRY:-https://registry.npmmirror.com}"
HARBOR_OPENSANDBOX_GOPROXY="${HARBOR_OPENSANDBOX_GOPROXY:-https://goproxy.cn,direct}"
HARBOR_OPENSANDBOX_GOSUMDB="${HARBOR_OPENSANDBOX_GOSUMDB:-sum.golang.google.cn}"
HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL="${HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL:-sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/}"
HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER="${HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER:-https://mirrors.tuna.tsinghua.edu.cn/rustup}"
HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT="${HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT:-https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup}"
HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL="${HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL:-}"
HARBOR_OPENSANDBOX_RUSTUP_INIT_URL="${HARBOR_OPENSANDBOX_RUSTUP_INIT_URL:-}"
HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL="${HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL:-}"
HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL="${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL:-}"
HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON:-}"
if [[ -z "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}" ]]; then
  HARBOR_OPENSANDBOX_BUILD_ARGS_JSON='{}'
fi
HARBOR_OPENSANDBOX_BUILD_USE_PROXY="${HARBOR_OPENSANDBOX_BUILD_USE_PROXY:-1}"
HARBOR_OPENSANDBOX_BUILD_NETWORK="${HARBOR_OPENSANDBOX_BUILD_NETWORK:-host}"
HARBOR_OPENSANDBOX_BUILD_PROXY_URL="${HARBOR_OPENSANDBOX_BUILD_PROXY_URL:-}"
HARBOR_OPENSANDBOX_IMAGE_MANAGER="${HARBOR_OPENSANDBOX_IMAGE_MANAGER:-$SCRIPT_DIR/opensandbox_image_manager.py}"
