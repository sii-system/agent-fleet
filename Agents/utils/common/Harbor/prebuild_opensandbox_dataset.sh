#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Use the same project configuration and runtime-override precedence as the
# normal Agent Fleet entry points.
# shellcheck source=../../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

DATASET_ROOT="${1:?usage: prebuild_opensandbox_dataset.sh DATASET_ROOT BENCHMARK_NAME}"
BENCHMARK_NAME="${2:?usage: prebuild_opensandbox_dataset.sh DATASET_ROOT BENCHMARK_NAME}"
YICLOUD_HARBOR_HOST="${YICLOUD_HARBOR_HOST:-}"
YICLOUD_HARBOR_PROJECT="${YICLOUD_HARBOR_PROJECT:-}"
YICLOUD_HARBOR_TLS_VERIFY="${YICLOUD_HARBOR_TLS_VERIFY:-0}"
HARBOR_OPENSANDBOX_DOCKER_CONFIG="${HARBOR_OPENSANDBOX_DOCKER_CONFIG:-${HOME}/.docker/config.json}"
HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT="${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT:-/data/harbor-runs/opensandbox-images}"
HARBOR_OPENSANDBOX_IMAGE_PLATFORM="${HARBOR_OPENSANDBOX_IMAGE_PLATFORM:-linux/amd64}"
HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC="${HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC:-7200}"
HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX="${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX:-m.daocloud.io/docker.io}"
DOMESTIC_APT_MIRROR="http://mirrors.tuna.tsinghua.edu.cn"
DOMESTIC_PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
DOMESTIC_NPM_REGISTRY="https://registry.npmmirror.com"
DOMESTIC_GOPROXY="https://goproxy.cn,direct"
DOMESTIC_GOSUMDB="sum.golang.google.cn"
DOMESTIC_CARGO_REGISTRY_URL="sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"
DOMESTIC_RUSTUP_DIST_SERVER="https://mirrors.tuna.tsinghua.edu.cn/rustup"
DOMESTIC_RUSTUP_UPDATE_ROOT="https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"
HARBOR_OPENSANDBOX_APT_MIRROR="${HARBOR_OPENSANDBOX_APT_MIRROR:-${DOMESTIC_APT_MIRROR}}"
HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS="${HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS:-https://mirrors.tuna.tsinghua.edu.cn,https://mirrors.aliyun.com}"
HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON="${HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON:-}"
if [[ -z "${HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON}" ]]; then
  HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON='{}'
fi
HARBOR_OPENSANDBOX_PIP_INDEX_URL="${HARBOR_OPENSANDBOX_PIP_INDEX_URL:-${DOMESTIC_PIP_INDEX_URL}}"
HARBOR_OPENSANDBOX_NPM_REGISTRY="${HARBOR_OPENSANDBOX_NPM_REGISTRY:-${DOMESTIC_NPM_REGISTRY}}"
HARBOR_OPENSANDBOX_GOPROXY="${HARBOR_OPENSANDBOX_GOPROXY:-${DOMESTIC_GOPROXY}}"
HARBOR_OPENSANDBOX_GOSUMDB="${HARBOR_OPENSANDBOX_GOSUMDB:-${DOMESTIC_GOSUMDB}}"
HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL="${HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL:-${DOMESTIC_CARGO_REGISTRY_URL}}"
HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER="${HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER:-${DOMESTIC_RUSTUP_DIST_SERVER}}"
HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT="${HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT:-${DOMESTIC_RUSTUP_UPDATE_ROOT}}"
HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL="${HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL:-}"
HARBOR_OPENSANDBOX_RUSTUP_INIT_URL="${HARBOR_OPENSANDBOX_RUSTUP_INIT_URL:-}"
HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL="${HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL:-}"
HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL="${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL:-}"
HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC="${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC:-5}"
HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON:-}"
if [[ -z "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}" ]]; then
  HARBOR_OPENSANDBOX_BUILD_ARGS_JSON='{}'
fi
# Task Dockerfiles commonly fetch release artifacts directly from upstream
# hosts.  Route those build-time fetches through the explicitly enabled
# development-machine proxy by default.  `host` is required when proxy_on
# provides a loopback listener to the BuildKit worker.
HARBOR_OPENSANDBOX_BUILD_USE_PROXY="${HARBOR_OPENSANDBOX_BUILD_USE_PROXY:-1}"
HARBOR_OPENSANDBOX_BUILD_NETWORK="${HARBOR_OPENSANDBOX_BUILD_NETWORK:-host}"
HARBOR_OPENSANDBOX_BUILD_PROXY_URL="${HARBOR_OPENSANDBOX_BUILD_PROXY_URL:-}"
HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY="${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY:-1}"
HARBOR_OPENSANDBOX_DRY_RUN="${HARBOR_OPENSANDBOX_DRY_RUN:-0}"
HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE="${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE:-1}"
HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION="${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION:-0}"
HARBOR_OPENSANDBOX_PREBUILD_ROOT="${HARBOR_OPENSANDBOX_PREBUILD_ROOT:-/data/harbor-runs/opensandbox-prebuild}"
HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC="${HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC:-1800}"
HARBOR_OPENSANDBOX_PREBUILD_GC_MAX_USED_SPACE="${HARBOR_OPENSANDBOX_PREBUILD_GC_MAX_USED_SPACE:-500GB}"
HARBOR_OPENSANDBOX_PREBUILD_GC_MIN_FREE_SPACE="${HARBOR_OPENSANDBOX_PREBUILD_GC_MIN_FREE_SPACE:-300GB}"
HARBOR_OPENSANDBOX_PREBUILD_GC_RESERVED_SPACE="${HARBOR_OPENSANDBOX_PREBUILD_GC_RESERVED_SPACE:-100GB}"
HARBOR_OPENSANDBOX_IMAGE_MANAGER="${HARBOR_OPENSANDBOX_IMAGE_MANAGER:-${SCRIPT_DIR}/opensandbox_image_manager.py}"
if [[ -z "${HARBOR_OPENSANDBOX_MANAGER_PYTHON:-}" ]]; then
  if [[ -n "${HARBOR_OPIK_PYTHON:-}" ]]; then
    HARBOR_OPENSANDBOX_MANAGER_PYTHON="${HARBOR_OPIK_PYTHON}"
  elif [[ -x /opt/harbor-runner/bin/python ]]; then
    HARBOR_OPENSANDBOX_MANAGER_PYTHON=/opt/harbor-runner/bin/python
  elif [[ -x "${HOME}/.local/share/agent-fleet/harbor-runner/bin/python" ]]; then
    HARBOR_OPENSANDBOX_MANAGER_PYTHON="${HOME}/.local/share/agent-fleet/harbor-runner/bin/python"
  else
    HARBOR_OPENSANDBOX_MANAGER_PYTHON=python3
  fi
fi

append_direct_host() {
  local value="$1"
  local host="${value#*://}"
  local merged="${NO_PROXY:-${no_proxy:-}}"
  host="${host%%/*}"
  host="${host%%:*}"
  [[ -n "${host}" ]] || return 0
  case ",${merged}," in
    *,"${host}",*) ;;
    *) merged="${merged:+${merged},}${host}" ;;
  esac
  NO_PROXY="${merged}"
  no_proxy="${merged}"
  export NO_PROXY no_proxy
}

# Registry control traffic is host-side transport, not a Dockerfile build arg.
# Keep the selected OCI Registry and base-image mirror direct even when the
# prebuild shell has an HTTP(S) proxy for difficult upstream package sources.
append_direct_host "${YICLOUD_HARBOR_HOST}"
append_direct_host "${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX}"

color_enabled() {
  [[ -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb && -t "$1" ]]
}

print_error() {
  if color_enabled 2; then
    printf '\033[31m%s\033[0m\n' "$*" >&2
  else
    printf '%s\n' "$*" >&2
  fi
}

print_warning() {
  if color_enabled 2; then
    printf '\033[33m%s\033[0m\n' "$*" >&2
  else
    printf '%s\n' "$*" >&2
  fi
}

[[ -n "${YICLOUD_HARBOR_HOST}" ]] || {
  print_error "[ERROR] set YICLOUD_HARBOR_HOST in config.env or config.local.env"
  exit 1
}
[[ -n "${YICLOUD_HARBOR_PROJECT}" ]] || {
  print_error "[ERROR] set externally provisioned YICLOUD_HARBOR_PROJECT in config.local.env"
  exit 1
}

colorize_prebuild_output() {
  local line
  local use_color=0
  color_enabled 1 && use_color=1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${use_color}" == 1 \
      && ("${line}" == *"[ERROR]"* || "${line}" == *"[failed]"*) ]]; then
      printf '\033[31m%s\033[0m\n' "${line}"
    elif [[ "${use_color}" == 1 \
      && ("${line}" == *"[WARN]"* || "${line}" == *"[warning]"*) ]]; then
      printf '\033[33m%s\033[0m\n' "${line}"
    else
      printf '%s\n' "${line}"
    fi
  done
}

case "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" in
  ''|*[!0-9]*|0)
    print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY must be positive"
    exit 1
    ;;
esac
case "${HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC}" in
  ''|*[!0-9]*|0)
    print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC must be a positive integer"
    exit 1
    ;;
esac
case "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_BUILD_USE_PROXY must be 0 or 1"; exit 1 ;;
esac
case "${HARBOR_OPENSANDBOX_BUILD_NETWORK}" in
  default|host) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_BUILD_NETWORK must be default or host"; exit 1 ;;
esac
case "${HARBOR_OPENSANDBOX_DRY_RUN}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_DRY_RUN must be 0 or 1"; exit 1 ;;
esac
case "${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE must be 0 or 1"; exit 1 ;;
esac
case "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION must be 0 or 1"; exit 1 ;;
esac
if [[ "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" == 1 \
  && "${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE}" != 1 ]]; then
  print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION=1 requires HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE=1"
  exit 1
fi
case "${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC}" in
  ''|*[!0-9]*|0)
    print_error "[ERROR] HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC must be a positive integer"
    exit 1
    ;;
esac
case "${HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC}" in
  ''|*[!0-9]*)
    print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC must be a non-negative integer"
    exit 1
    ;;
esac
for gc_space_name in \
  HARBOR_OPENSANDBOX_PREBUILD_GC_MAX_USED_SPACE \
  HARBOR_OPENSANDBOX_PREBUILD_GC_MIN_FREE_SPACE \
  HARBOR_OPENSANDBOX_PREBUILD_GC_RESERVED_SPACE; do
  gc_space_value="${!gc_space_name}"
  if [[ ! "${gc_space_value}" =~ ^[0-9]+([.][0-9]+)?([kKmMgGtT][bB]?)?$ ]]; then
    print_error "[ERROR] ${gc_space_name} must be a non-negative byte value such as 500GB"
    exit 1
  fi
done

package_sources_healthy() {
  [[ -n "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" ]] || return 0
  curl --noproxy '*' --fail --silent --show-error \
    --max-time "${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC}" \
    "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" >/dev/null 2>&1
}

use_domestic_package_sources() {
  HARBOR_OPENSANDBOX_PIP_INDEX_URL="${DOMESTIC_PIP_INDEX_URL}"
  HARBOR_OPENSANDBOX_NPM_REGISTRY="${DOMESTIC_NPM_REGISTRY}"
  HARBOR_OPENSANDBOX_GOPROXY="${DOMESTIC_GOPROXY}"
  HARBOR_OPENSANDBOX_GOSUMDB="${DOMESTIC_GOSUMDB}"
  HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL="${DOMESTIC_CARGO_REGISTRY_URL}"
  HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER="${DOMESTIC_RUSTUP_DIST_SERVER}"
  HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT="${DOMESTIC_RUSTUP_UPDATE_ROOT}"
  HARBOR_OPENSANDBOX_RUSTUP_INIT_URL=""
  HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL=""
}

select_domestic_apt_mirror() {
  local candidate
  local candidates="${HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS}"
  while IFS= read -r candidate; do
    candidate="${candidate%/}"
    [[ "${candidate}" =~ ^https://[^/?#]+$ ]] || continue
    if curl --noproxy '*' --fail --silent --show-error --head \
      --max-time "${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC}" \
      "${candidate}/ubuntu/dists/jammy/InRelease" >/dev/null 2>&1; then
      printf '%s' "${candidate}"
      return 0
    fi
  done < <(printf '%s' "${candidates}" | tr ',' '\n')
  return 1
}

# Fast resume returns from the image manager before package-source validation.
# Defer health traffic in that mode so a fully local batch performs no network
# request merely to discover its cache hits. A real cache miss still probes and
# switches sources through the existing failed-build fallback below.
if [[ "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" != 1 ]] \
  && ! package_sources_healthy; then
  print_warning \
    "[WARN] configured package sources are unavailable; using trusted domestic defaults"
  if ! HARBOR_OPENSANDBOX_APT_MIRROR="$(select_domestic_apt_mirror)"; then
    print_error "[ERROR] no configured trusted domestic APT fallback is reachable"
    exit 1
  fi
  use_domestic_package_sources
  HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL=""
fi

DATASET_ROOT="$(cd "${DATASET_ROOT}" && pwd)"
[[ -f "${HARBOR_OPENSANDBOX_IMAGE_MANAGER}" ]] || {
  print_error "[ERROR] image manager not found: ${HARBOR_OPENSANDBOX_IMAGE_MANAGER}"
  exit 1
}
[[ -f "${HARBOR_OPENSANDBOX_DOCKER_CONFIG}" || "${HARBOR_OPENSANDBOX_DRY_RUN}" == 1 ]] || {
  print_error "[ERROR] Docker config not found: ${HARBOR_OPENSANDBOX_DOCKER_CONFIG}"
  exit 1
}
command -v "${HARBOR_OPENSANDBOX_MANAGER_PYTHON}" >/dev/null 2>&1 || {
  print_error "[ERROR] Python not found: ${HARBOR_OPENSANDBOX_MANAGER_PYTHON}"
  exit 1
}
if [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" != 1 ]] \
  && ! docker buildx version >/dev/null 2>&1; then
  print_error "[ERROR] docker buildx is required to prebuild benchmark task images."
  print_error "[ERROR] Install Docker Buildx, then verify: docker buildx version"
  exit 1
fi
if [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" != 1 \
  && "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" == 1 ]]; then
  proxy_names=(HTTP_PROXY HTTPS_PROXY http_proxy https_proxy)
  if [[ -n "${HARBOR_OPENSANDBOX_BUILD_PROXY_URL}" ]]; then
    proxy_names=(HARBOR_OPENSANDBOX_BUILD_PROXY_URL)
  fi
  for proxy_name in "${proxy_names[@]}"; do
    proxy_value="${!proxy_name:-}"
    case "${proxy_value}" in
      *://127.0.0.1:*|*://127.0.0.1|*://localhost:*|*://localhost|*://\[::1\]:*|*://\[::1\])
        [[ "${HARBOR_OPENSANDBOX_BUILD_NETWORK}" == host ]] && continue
        print_error \
          "[ERROR] ${proxy_name} is a loopback proxy and is unreachable from BuildKit containers."
        print_error \
          "[ERROR] Set HARBOR_OPENSANDBOX_BUILD_NETWORK=host or use a container-reachable proxy address."
        exit 1
        ;;
    esac
  done
fi

run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="${HARBOR_OPENSANDBOX_PREBUILD_ROOT}/${BENCHMARK_NAME}-${run_stamp}"
supported_nul="${run_dir}/supported.nul"
supported_txt="${run_dir}/supported.txt"
skipped_txt="${run_dir}/skipped.txt"
run_log="${run_dir}/prebuild.log"
gc_log="${run_dir}/buildkit-gc.log"
mkdir -p "${run_dir}"
mkdir -p "${run_dir}/bundles"
: > "${supported_nul}"
: > "${supported_txt}"
: > "${skipped_txt}"
: > "${gc_log}"

run_buildkit_gc() {
  printf '[%s] docker buildx prune start\n' "$(date -u +%FT%TZ)" >> "${gc_log}"
  if docker buildx prune --force \
    --max-used-space "${HARBOR_OPENSANDBOX_PREBUILD_GC_MAX_USED_SPACE}" \
    --min-free-space "${HARBOR_OPENSANDBOX_PREBUILD_GC_MIN_FREE_SPACE}" \
    --reserved-space "${HARBOR_OPENSANDBOX_PREBUILD_GC_RESERVED_SPACE}" \
    >> "${gc_log}" 2>&1; then
    printf '[%s] docker buildx prune complete\n' "$(date -u +%FT%TZ)" >> "${gc_log}"
  else
    printf '[%s] docker buildx prune failed; prebuild continues\n' \
      "$(date -u +%FT%TZ)" >> "${gc_log}"
    print_warning "[WARN] BuildKit cache prune failed; details=${gc_log}"
  fi
}

gc_pid=""
stop_periodic_gc() {
  if [[ -n "${gc_pid}" ]]; then
    kill "${gc_pid}" >/dev/null 2>&1 || true
    wait "${gc_pid}" 2>/dev/null || true
  fi
}
trap stop_periodic_gc EXIT

if [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" != 1 ]]; then
  run_buildkit_gc
  if [[ "${HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC}" != 0 ]]; then
    (
      while sleep "${HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC}"; do
        run_buildkit_gc
      done
    ) &
    gc_pid="$!"
  fi
fi

for task_dir in "${DATASET_ROOT}"/*; do
  [[ -d "${task_dir}" ]] || continue
  task_name="$(basename "${task_dir}")"
  if [[ ! -f "${task_dir}/task.toml" ]]; then
    printf '%s\tmissing-task.toml\n' "${task_name}" >> "${skipped_txt}"
  elif [[ ! -f "${task_dir}/environment/Dockerfile" \
    && ! -f "${task_dir}/environment/docker-compose.yml" \
    && ! -f "${task_dir}/environment/docker-compose.yaml" ]]; then
    printf '%s\tmissing-environment-definition\n' "${task_name}" >> "${skipped_txt}"
  else
    printf '%s\0' "${task_dir}" >> "${supported_nul}"
    printf '%s\n' "${task_name}" >> "${supported_txt}"
  fi
done

supported_count="$(wc -l < "${supported_txt}" | tr -d ' ')"
skipped_count="$(wc -l < "${skipped_txt}" | tr -d ' ')"
printf '[prebuild] benchmark=%s supported=%s skipped=%s concurrency=%s dry_run=%s\n' \
  "${BENCHMARK_NAME}" "${supported_count}" "${skipped_count}" \
  "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" "${HARBOR_OPENSANDBOX_DRY_RUN}"
printf '[prebuild] registry=%s project=%s (task repositories are derived per task)\n' \
  "${YICLOUD_HARBOR_HOST}" "${YICLOUD_HARBOR_PROJECT}"
local_upload_hash_verification=disabled
if [[ "${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE}" == 1 ]]; then
  local_upload_hash_verification=enabled
  if [[ "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" == 1 ]]; then
    local_upload_hash_verification=skipped
  fi
fi
printf '[prebuild] local_upload_cache=%s hash_verification=%s cache_root=%s\n' \
  "$([[ "${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE}" == 1 ]] && printf enabled || printf disabled)" \
  "${local_upload_hash_verification}" \
  "${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT}"
printf '[prebuild] package_sources=%s apt_mirror=%s fallback_apt_mirrors=%s\n' \
  "$([[ -n "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" ]] && printf configured || printf domestic-defaults)" \
  "${HARBOR_OPENSANDBOX_APT_MIRROR}" \
  "${HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS}"
printf '[prebuild] github_mirror=%s\n' \
  "$([[ -n "${HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL}" ]] && printf configured || printf disabled)"
printf '[prebuild] run_dir=%s\n' "${run_dir}"
if [[ "${skipped_count}" != 0 ]]; then
  print_warning \
    "[WARN] skipped ${skipped_count} unsupported tasks; details=${skipped_txt}"
fi

export BENCHMARK_NAME
export YICLOUD_HARBOR_HOST YICLOUD_HARBOR_PROJECT YICLOUD_HARBOR_TLS_VERIFY
export HARBOR_OPENSANDBOX_DOCKER_CONFIG
export HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT
export HARBOR_OPENSANDBOX_IMAGE_PLATFORM
export HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC
export HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX
export HARBOR_OPENSANDBOX_APT_MIRROR
export HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS
export HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON
export HARBOR_OPENSANDBOX_PIP_INDEX_URL HARBOR_OPENSANDBOX_NPM_REGISTRY
export HARBOR_OPENSANDBOX_GOPROXY HARBOR_OPENSANDBOX_GOSUMDB
export HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL
export HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT
export HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL
export HARBOR_OPENSANDBOX_RUSTUP_INIT_URL HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL
export HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL
export DOMESTIC_PIP_INDEX_URL DOMESTIC_NPM_REGISTRY DOMESTIC_GOPROXY
export DOMESTIC_GOSUMDB DOMESTIC_CARGO_REGISTRY_URL
export DOMESTIC_RUSTUP_DIST_SERVER DOMESTIC_RUSTUP_UPDATE_ROOT
export HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC
export HARBOR_OPENSANDBOX_BUILD_ARGS_JSON
export HARBOR_OPENSANDBOX_BUILD_USE_PROXY
export HARBOR_OPENSANDBOX_BUILD_NETWORK
export HARBOR_OPENSANDBOX_BUILD_PROXY_URL
export HARBOR_OPENSANDBOX_DRY_RUN
export HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE
export HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION
export HARBOR_OPENSANDBOX_IMAGE_MANAGER
export HARBOR_OPENSANDBOX_MANAGER_PYTHON
export run_dir

set +e
xargs -0 -r -P "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" -n 1 \
  bash -c '
    task_dir="$1"
    task_name="$(basename "${task_dir}")"
    package_sources_healthy() {
      [[ -n "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" ]] || return 0
      curl --noproxy "*" --fail --silent --show-error \
        --max-time "${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC}" \
        "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" >/dev/null 2>&1
    }
    use_domestic_package_sources() {
      HARBOR_OPENSANDBOX_PIP_INDEX_URL="${DOMESTIC_PIP_INDEX_URL}"
      HARBOR_OPENSANDBOX_NPM_REGISTRY="${DOMESTIC_NPM_REGISTRY}"
      HARBOR_OPENSANDBOX_GOPROXY="${DOMESTIC_GOPROXY}"
      HARBOR_OPENSANDBOX_GOSUMDB="${DOMESTIC_GOSUMDB}"
      HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL="${DOMESTIC_CARGO_REGISTRY_URL}"
      HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER="${DOMESTIC_RUSTUP_DIST_SERVER}"
      HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT="${DOMESTIC_RUSTUP_UPDATE_ROOT}"
      HARBOR_OPENSANDBOX_RUSTUP_INIT_URL=""
      HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL=""
      export HARBOR_OPENSANDBOX_PIP_INDEX_URL HARBOR_OPENSANDBOX_NPM_REGISTRY
      export HARBOR_OPENSANDBOX_GOPROXY HARBOR_OPENSANDBOX_GOSUMDB
      export HARBOR_OPENSANDBOX_CARGO_REGISTRY_URL
      export HARBOR_OPENSANDBOX_RUSTUP_DIST_SERVER HARBOR_OPENSANDBOX_RUSTUP_UPDATE_ROOT
      export HARBOR_OPENSANDBOX_RUSTUP_INIT_URL HARBOR_OPENSANDBOX_PYTORCH_INDEX_URL
    }
    select_domestic_apt_mirror() {
      local candidate
      while IFS= read -r candidate; do
        candidate="${candidate%/}"
        [[ "${candidate}" =~ ^https://[^/?#]+$ ]] || continue
        if curl --noproxy "*" --fail --silent --show-error --head \
          --max-time "${HARBOR_OPENSANDBOX_APT_MIRROR_PROBE_TIMEOUT_SEC}" \
          "${candidate}/ubuntu/dists/jammy/InRelease" >/dev/null 2>&1; then
          printf "%s" "${candidate}"
          return 0
        fi
      done < <(printf "%s" "${HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS}" | tr "," "\n")
      return 1
    }
    build_command() {
      local apt_mirror="$1"
      command=(
        "${HARBOR_OPENSANDBOX_MANAGER_PYTHON}"
        "${HARBOR_OPENSANDBOX_IMAGE_MANAGER}"
        --task-dir "${task_dir}"
        --registry "${YICLOUD_HARBOR_HOST}"
        --project "${YICLOUD_HARBOR_PROJECT}"
        --benchmark-name "${BENCHMARK_NAME}"
        --docker-config "${HARBOR_OPENSANDBOX_DOCKER_CONFIG}"
        --cache-root "${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT}"
        --platform "${HARBOR_OPENSANDBOX_IMAGE_PLATFORM}"
        --build-timeout-sec "${HARBOR_OPENSANDBOX_PREBUILD_BUILD_TIMEOUT_SEC}"
        --tag-prefix "${BENCHMARK_NAME}"
        --dockerhub-mirror-prefix "${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX}"
        --apt-mirror "${apt_mirror}"
        --apt-source-overrides-json "${HARBOR_OPENSANDBOX_APT_SOURCE_OVERRIDES_JSON}"
        --build-args-json "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}"
        --build-network "${HARBOR_OPENSANDBOX_BUILD_NETWORK}"
        --bundle-manifest-output "${run_dir}/bundles/${task_name}.json"
        --retry-no-cache-on-apt-404
      )
      [[ "${YICLOUD_HARBOR_TLS_VERIFY}" == 1 ]] && command+=(--registry-tls-verify)
      [[ "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" == 1 ]] && command+=(--use-proxy)
      [[ "${HARBOR_OPENSANDBOX_PREBUILD_USE_LOCAL_UPLOAD_CACHE}" == 1 ]] && command+=(--reuse-local-upload-cache)
      [[ "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" == 1 ]] && command+=(--skip-hash-verification)
      [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" == 1 ]] && command+=(--dry-run)
    }
    active_apt_mirror="${HARBOR_OPENSANDBOX_APT_MIRROR}"
    if [[ "${HARBOR_OPENSANDBOX_PREBUILD_SKIP_HASH_VERIFICATION}" != 1 ]] \
      && ! package_sources_healthy; then
      if ! active_apt_mirror="$(select_domestic_apt_mirror)"; then
        printf "[prebuild][failed] task=%s configured package sources are unavailable and no trusted domestic APT fallback is reachable\n" \
          "${task_name}" >&2
        exit 1
      fi
      use_domestic_package_sources
      printf "[prebuild][warning] task=%s configured package sources are unavailable before build; using trusted domestic defaults\n" \
        "${task_name}" >&2
    fi
    build_command "${active_apt_mirror}"
    if image_ref="$("${command[@]}")"; then
      printf "[prebuild][ready] task=%s image_ref=%s bundle=%s\n" \
        "${task_name}" "${image_ref}" "${run_dir}/bundles/${task_name}.json"
    elif [[ -n "${HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL}" ]] \
      && ! package_sources_healthy; then
      printf "[prebuild][warning] task=%s configured package sources became unavailable; retrying with trusted domestic defaults\n" \
        "${task_name}" >&2
      if ! fallback_mirror="$(select_domestic_apt_mirror)"; then
        printf "[prebuild][failed] task=%s no trusted domestic APT fallback is reachable\n" \
          "${task_name}" >&2
        exit 1
      fi
      use_domestic_package_sources
      build_command "${fallback_mirror}"
      if image_ref="$("${command[@]}")"; then
        printf "[prebuild][ready-fallback] task=%s image_ref=%s bundle=%s\n" \
          "${task_name}" "${image_ref}" "${run_dir}/bundles/${task_name}.json"
      else
        printf "[prebuild][failed] task=%s\n" "${task_name}" >&2
        exit 1
      fi
    else
      printf "[prebuild][failed] task=%s\n" "${task_name}" >&2
      exit 1
    fi
  ' _ < "${supported_nul}" 2>&1 | tee "${run_log}" | colorize_prebuild_output
xargs_status="${PIPESTATUS[0]}"
set -e

if [[ "${xargs_status}" != 0 ]]; then
  print_error "[ERROR] one or more task images failed; rerun the same command to resume"
  print_error "[ERROR] log=${run_log}"
  exit "${xargs_status}"
fi

printf '[prebuild] complete; log=%s skipped=%s\n' "${run_log}" "${skipped_txt}"
