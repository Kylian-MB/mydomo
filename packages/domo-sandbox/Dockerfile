# The sandbox: a launcher that runs one tool of the assistant in a process of its own, with limits. The
# container has no network at all; the core talks to it through a Unix socket in a shared volume.
#
# Build from the repository root:
#   docker build -f packages/domo-sandbox/Dockerfile .
#
# Both base images are pinned by digest (the tag is kept for readers). To update a pin, look up
# the digest of the tag with `docker buildx imagetools inspect <image>:<tag>`.

# --- Build: the virtual environment, with the launcher and its dependencies (Bandit included) --
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
# when they change. Only the packages the launcher needs are copied.
COPY pyproject.toml uv.lock ./
COPY packages/domo-common/pyproject.toml packages/domo-common/pyproject.toml
COPY packages/domo-sandbox/pyproject.toml packages/domo-sandbox/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --no-install-workspace --package domo-sandbox

# Then the sources, installed into the environment itself (not editable): the runtime image
# needs no source tree.
COPY packages/domo-common/src packages/domo-common/src
COPY packages/domo-sandbox/src packages/domo-sandbox/src
RUN uv sync --frozen --no-dev --no-editable --package domo-sandbox

# --- Runtime ---------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The launcher starts as root and stays so: it gives each run its own account (20000-29999) and
# its own limits, and only it keeps SETUID/SETGID. The code of a tool never runs as root.
WORKDIR /app

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, socket; s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(os.environ.get('DOMO_SANDBOX_SOCKET', '/run/domo/sandbox/launcher.sock')); s.sendall(b'GET /health HTTP/1.0\\r\\n\\r\\n'); raise SystemExit(0 if b' 200 ' in s.recv(64) else 1)"]

CMD ["python", "-m", "domo_sandbox"]
