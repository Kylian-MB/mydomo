# The core: the web page, the chat, the accounts and the audit trail. It has no way to the
# Internet: the compose file attaches it to the internal network only. The same image runs the
# one-shot `init` service (`python -m domo_core.init_secrets`).
#
# Build from the repository root:
#   docker build -f packages/domo-core/Dockerfile .
#
# Both base images are pinned by digest (the tag is kept for readers), the same as the
# gatekeeper's. To update a pin, look up the digest of the tag with
# `docker buildx imagetools inspect <image>:<tag>`.

# --- Build: the virtual environment, with the core and its dependencies ----------------------
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
# when they change. Only the packages the core needs are copied.
COPY pyproject.toml uv.lock ./
COPY packages/domo-common/pyproject.toml packages/domo-common/pyproject.toml
COPY packages/domo-core/pyproject.toml packages/domo-core/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --no-install-workspace --package domo-core

# Then the sources, installed into the environment itself (not editable): the runtime image
# needs no source tree. The migrations, templates and static files are part of the package.
COPY packages/domo-common/src packages/domo-common/src
COPY packages/domo-core/src packages/domo-core/src
RUN uv sync --frozen --no-dev --no-editable --package domo-core

# The local model that gives the meaning of memories, downloaded now: the running core has no way
# to the Internet and its root filesystem is read-only, so it only ever reads this folder.
RUN /app/.venv/bin/python -c "from pathlib import Path; from domo_core.memory.embedder import fetch_model; fetch_model(Path('/opt/domo/models'))" \
    && chmod -R a+rX /opt/domo/models

# --- Runtime ---------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# An unprivileged user (the gatekeeper's uid and gid: both read the core token as its owner),
# and the mount points of the two secret volumes. A new named volume takes the ownership and
# mode of its mount point: root, 0755, so that `init` (root) writes there and each reader can
# reach its own file.
RUN groupadd --system --gid 10001 domo \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin domo \
    && install -d -m 0755 /run/domo/db-auth /run/domo/core-auth

COPY --from=build /app/.venv /app/.venv
# Owned by root, readable by everyone: the core reads it, nothing writes it.
COPY --from=build /opt/domo/models /opt/domo/models

# The model is never looked for on the network (``memory.embedder`` asks for local files only),
# and its runtime keeps no telemetry (it would warn that it cannot write its id).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    ORT_DISABLE_TELEMETRY=1

USER 10001:10001
WORKDIR /app

# The web page, behind the edge proxy (the core's network only).
EXPOSE 8000

# The start migrates the database first: a longer start period, checked every 2 s meanwhile.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["python", "-m", "domo_core"]
