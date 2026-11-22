# The gatekeeper: the encrypted secret vault, the provider relay and the only service with
# Internet egress.
#
# Build from the repository root:
#   docker build -f packages/domo-gatekeeper/Dockerfile .
#
# Both base images are pinned by digest (the tag is kept for readers). To update a pin, look up
# the digest of the tag with `docker buildx imagetools inspect <image>:<tag>`.

# --- Build: the virtual environment, with the gatekeeper and its dependencies ----------------
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /usr/local/bin/uv

# Compiled bytecode: the root filesystem is read-only when the container runs, so Python could
# not write it then. The interpreter is the image's own: nothing is downloaded.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first, from the manifests and the lock file alone: this layer is rebuilt only
# when they change. Only the packages the gatekeeper needs are copied.
COPY pyproject.toml uv.lock ./
COPY packages/domo-common/pyproject.toml packages/domo-common/pyproject.toml
COPY packages/domo-gatekeeper/pyproject.toml packages/domo-gatekeeper/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --no-install-workspace --package domo-gatekeeper

# Then the sources, installed into the environment itself (not editable): the runtime image
# needs no source tree.
COPY packages/domo-common/src packages/domo-common/src
COPY packages/domo-gatekeeper/src packages/domo-gatekeeper/src
RUN uv sync --frozen --no-dev --no-editable --package domo-gatekeeper

# --- Runtime ---------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# An unprivileged user, and the data directory it owns: a named volume mounted on /data starts
# out with the image's ownership and mode (0700, so no other user of the volume can read it).
RUN groupadd --system --gid 10001 domo \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin domo \
    && install -d -o 10001 -g 10001 -m 0700 /data

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /app

# 8081: the internal API (the core's network only). 8082: the vault form, behind the edge proxy.
EXPOSE 8081 8082

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8081/internal/health', timeout=3)"]

CMD ["python", "-m", "domo_gatekeeper"]
