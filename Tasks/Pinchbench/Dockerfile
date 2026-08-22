ARG OPENCLAW_BASE_IMAGE=openclaw:local
FROM ${OPENCLAW_BASE_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/pinchbench/skill"
LABEL org.opencontainers.image.description="PinchBench runner with OpenClaw CLI"

ARG PIP_INDEX_URL=""
ARG PIP_EXTRA_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG OPIK_TRACING=false

USER root

# The runner probes this marker before reuse so switching tracing modes cannot
# silently reuse an image built from the opposite OpenClaw base.
RUN printf '%s\n' "$OPIK_TRACING" > /opt/pinchbench-trace-to-opik

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
RUN ln -sf /root/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /root/.local/bin/uvx /usr/local/bin/uvx

# Keep this path aligned with Agents/Openclaw/scripts/setup.py. PinchBench worker
# containers use the same OpenClaw CLI version as the fleet gateway; a CLI/gateway
# protocol mismatch falls back to embedded execution and bypasses tracer uploads.
RUN if [ "$OPIK_TRACING" = "true" ]; then \
      python3 -m venv /opt/opik-venv \
      && if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi \
      && if [ -n "$PIP_EXTRA_INDEX_URL" ]; then export PIP_EXTRA_INDEX_URL; fi \
      && if [ -n "$PIP_TRUSTED_HOST" ]; then export PIP_TRUSTED_HOST; fi \
      && /opt/opik-venv/bin/pip install --no-cache-dir --quiet "opik>=1.0.0" uuid6 socksio \
      && chown -R root:root /opt/openclaw-plugins/openclaw-opik-tracer \
      && chmod -R a+rX /opt/openclaw-plugins/openclaw-opik-tracer; \
    fi

WORKDIR /workspace

ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["openclaw --version && uv --version && echo 'PinchBench runner ready'"]
