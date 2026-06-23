# Control-plane image for the Research Plugin cloud backend (plan Phase 9).
#
# Installs the `control` extra (Postgres driver + object-store SDK + provider
# SDK) and runs the control composition behind uvicorn. This is the REFERENCE
# stack — not a managed deploy: TLS termination, a real secret store, managed
# Postgres, and the cleanup scheduler are all the operator's responsibility (see
# deploy/README.md). Build context is the plugin root:
#   docker build -f deploy/Dockerfile -t research-plugin-control .
#
# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# No .pyc churn, unbuffered logs (so the structured JSON log stream reaches the
# platform's log pipeline immediately), pip lean.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is for the HEALTHCHECK probe. openssh-client is for the management SSH
# channel (Lambda transcript/metrics/parachute/secret delivery) and the local
# compose key-init job.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the project and install with the control extra. The `control` profile
# pulls psycopg + boto3 + the provider SDK (pyproject [project.optional-dependencies]).
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY mcp_server ./mcp_server
COPY research_plugin_shared ./research_plugin_shared
RUN pip install ".[control]"

# Run as a non-root user (the control plane never needs root; it holds no user
# checkout).
RUN useradd --create-home --uid 10001 rpcontrol
USER rpcontrol

ENV RESEARCH_PLUGIN_MODE=control \
    RESEARCH_PLUGIN_HTTP_HOST=0.0.0.0 \
    RESEARCH_PLUGIN_HTTP_PORT=8787

EXPOSE 8787

# Liveness via the unauthenticated version handshake (also covers /health). The
# control plane rejects below-floor clients but /api/meta is always open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/api/meta || exit 1

# control_main forces RESEARCH_PLUGIN_MODE=control (daemon endpoints on, reaper
# on). Keep it behind a TLS-terminating private boundary in production.
ENTRYPOINT ["research-plugin-control"]
