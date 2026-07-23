ARG DIND_BASE_IMAGE=m.daocloud.io/docker.io/library/debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
ARG UV_IMAGE=m.daocloud.io/ghcr.io/astral-sh/uv:0.11.28

FROM ${UV_IMAGE} AS uv
FROM ${DIND_BASE_IMAGE}

ARG DOCKER_CE_VERSION=5:28.5.2-1~debian.12~bookworm
ARG CONTAINERD_VERSION=2.2.6-1~debian.12~bookworm
ARG DOCKER_BUILDX_VERSION=0.35.0-1~debian.12~bookworm
ARG DOCKER_COMPOSE_VERSION=5.3.1-1~debian.12~bookworm
ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG DOCKER_APT_MIRROR=https://mirrors.aliyun.com/docker-ce/linux/debian
ARG NODE_VERSION=22.23.0
ARG NODE_DIST_MIRROR=https://registry.npmmirror.com/-/binary/node
ARG NODE_LINUX_X64_SHA256=14d7de44f235534799f8b171a4050d9a6a4bc99c87e053a25d3d54afa580aa20
ARG NODE_LINUX_ARM64_SHA256=4018815ac1bed4f18208901bbde524fee881253b591ee7bc952660e69bd057af
ARG ZELLIJ_VERSION=0.44.3
ARG ZELLIJ_RELEASE_BASE_URL=https://ghproxy.net/https://github.com/zellij-org/zellij/releases/download
ARG ZELLIJ_RELEASE_FALLBACK_BASE_URL=https://github.com/zellij-org/zellij/releases/download
ARG ZELLIJ_LINUX_X64_SHA256=0f7c346788627f506c0a28296517768633cff24fc822a739f8264b640ecad751
ARG ZELLIJ_LINUX_ARM64_SHA256=15e6534d42644d66973d136c590c49739dcfd6a1a2a0d3d917973f16c81b45fb
ARG RUNNER_PYTHON_VERSION=3.12.13
ARG PYTHON_INSTALL_MIRROR=https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    printf '%s\n' \
      'Types: deb' \
      "URIs: ${DEBIAN_MIRROR}" \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      "URIs: ${DEBIAN_SECURITY_MIRROR}" \
      'Suites: bookworm-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      coreutils \
      curl \
      e2fsprogs \
      findutils \
      git \
      gnupg \
      gzip \
      iproute2 \
      iptables \
      jq \
      kmod \
      libatomic1 \
      openssl \
      passwd \
      pigz \
      procps \
      python3 \
      tar \
      tini \
      tmux \
      util-linux \
      xfsprogs \
      xz-utils; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL \
      "${DOCKER_APT_MIRROR}/gpg" \
      -o /etc/apt/keyrings/docker.asc; \
    chmod a+r /etc/apt/keyrings/docker.asc; \
    arch="$(dpkg --print-architecture)"; \
    . /etc/os-release; \
    printf '%s\n' \
      "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] ${DOCKER_APT_MIRROR} ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "containerd.io=${CONTAINERD_VERSION}" \
      "docker-buildx-plugin=${DOCKER_BUILDX_VERSION}" \
      "docker-ce-cli=${DOCKER_CE_VERSION}" \
      "docker-ce=${DOCKER_CE_VERSION}" \
      "docker-compose-plugin=${DOCKER_COMPOSE_VERSION}"; \
    docker --version; \
    docker compose version; \
    dockerd --version; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) node_arch="x64"; node_sha256="${NODE_LINUX_X64_SHA256}" ;; \
      arm64) node_arch="arm64"; node_sha256="${NODE_LINUX_ARM64_SHA256}" ;; \
      *) echo "Unsupported Node architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    node_tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL \
      "${NODE_DIST_MIRROR}/v${NODE_VERSION}/${node_tarball}" \
      -o "/tmp/${node_tarball}"; \
    printf '%s  %s\n' "${node_sha256}" "/tmp/${node_tarball}" | sha256sum -c -; \
    tar -xJf "/tmp/${node_tarball}" --strip-components=1 -C /usr/local; \
    rm -f "/tmp/${node_tarball}"; \
    node --version; \
    npm --version

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) zellij_arch="x86_64"; zellij_sha256="${ZELLIJ_LINUX_X64_SHA256}" ;; \
      arm64) zellij_arch="aarch64"; zellij_sha256="${ZELLIJ_LINUX_ARM64_SHA256}" ;; \
      *) echo "Unsupported zellij architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    zellij_archive="zellij-${zellij_arch}-unknown-linux-musl.tar.gz"; \
    if ! curl --retry 2 --retry-delay 2 --retry-all-errors \
      --connect-timeout 8 --max-time 90 -fsSL \
      "${ZELLIJ_RELEASE_BASE_URL}/v${ZELLIJ_VERSION}/${zellij_archive}" \
      -o "/tmp/${zellij_archive}"; then \
      echo "Zellij mirror failed; falling back to the upstream release" >&2; \
      rm -f "/tmp/${zellij_archive}"; \
      curl --retry 5 --retry-delay 2 --retry-all-errors \
        --connect-timeout 10 --max-time 180 -fsSL \
        "${ZELLIJ_RELEASE_FALLBACK_BASE_URL}/v${ZELLIJ_VERSION}/${zellij_archive}" \
        -o "/tmp/${zellij_archive}"; \
    fi; \
    printf '%s  %s\n' "${zellij_sha256}" "/tmp/${zellij_archive}" | sha256sum -c -; \
    tar -xzf "/tmp/${zellij_archive}" -C /usr/local/bin zellij; \
    rm -f "/tmp/${zellij_archive}"; \
    chmod +x /usr/local/bin/zellij; \
    groupadd --system agent; \
    useradd --system --create-home --home-dir /home/agent --shell /bin/bash --gid agent agent; \
    usermod --append --groups docker agent; \
    python3 -c 'import pyexpat; print("system python", __import__("sys").version)'; \
    zellij --version

COPY --from=uv /uv /uvx /usr/local/bin/
COPY scripts/dind/dockerd-entrypoint.sh /usr/local/bin/dockerd-entrypoint.sh
COPY Agents/utils/common/Harbor/runner-requirements.txt /opt/harbor-runner-requirements.txt

ENV PATH="/opt/harbor-runner/bin:${PATH}" \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

RUN set -eux; \
    if ! timeout --signal=TERM --kill-after=10s 240s \
      env UV_HTTP_TIMEOUT=60 UV_PYTHON_DOWNLOADS=automatic \
        UV_PYTHON_INSTALL_MIRROR="${PYTHON_INSTALL_MIRROR}" \
        uv python install "${RUNNER_PYTHON_VERSION}"; then \
      echo "Python mirror failed; falling back to the upstream release" >&2; \
      timeout --signal=TERM --kill-after=10s 300s \
        env UV_HTTP_TIMEOUT=60 UV_PYTHON_DOWNLOADS=automatic \
          uv python install "${RUNNER_PYTHON_VERSION}"; \
    fi; \
    uv venv --python "${RUNNER_PYTHON_VERSION}" /opt/harbor-runner; \
    uv pip install \
      --only-binary :all: \
      --python /opt/harbor-runner/bin/python \
      --requirement /opt/harbor-runner-requirements.txt; \
    uv pip check --python /opt/harbor-runner/bin/python; \
    /opt/harbor-runner/bin/python -c 'import sys; assert sys.version_info[:3] == (3, 12, 13), sys.version'; \
    /opt/harbor-runner/bin/opik harbor run --help >/dev/null; \
    /opt/harbor-runner/bin/harbor run --help >/dev/null; \
    runuser -u agent -- /opt/harbor-runner/bin/python -c 'import harbor, opik'; \
    runuser -u agent -- /opt/harbor-runner/bin/opik harbor run --help >/dev/null; \
    runuser -u agent -- /opt/harbor-runner/bin/harbor run --help >/dev/null

RUN printf '%s\n' 'export PATH="/opt/harbor-runner/bin:$PATH"' \
      > /etc/profile.d/harbor-runner.sh

RUN set -eux; \
    update-alternatives --set iptables /usr/sbin/iptables-legacy; \
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy; \
    iptables --version; \
    ip6tables --version

ENTRYPOINT ["tini", "--", "/usr/local/bin/dockerd-entrypoint.sh"]
