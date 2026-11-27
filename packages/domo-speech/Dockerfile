# The speech service of the house: sound to text, text to sound, sound to voice print. It is
# reached by the gatekeeper alone (the compose file puts it on a network of its own) and writes
# only in /models (a volume), where it keeps the open model weights it fetches at its first start.
#
# Build from the repository root:
#   docker build -f packages/domo-speech/Dockerfile .
#   docker build -f packages/domo-speech/Dockerfile --build-arg SPEECH_EXTRAS="--extra models --extra gpu" .
#   (the second one: a graphics card)
#
# Both base images are pinned by digest (the tag is kept for readers), the same as the
# gatekeeper's. To update a pin, look up the digest of the tag with
# `docker buildx imagetools inspect <image>:<tag>`.

# --- Build: the virtual environment, with the service, its real engines and their dependencies
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first, from the manifests and the lock file alone. The extra `models` brings the
# real engines (CTranslate2 and ONNX Runtime); the model weights are not here: the service
# fetches them into /models at its first start.
ARG SPEECH_EXTRAS="--extra models"
COPY pyproject.toml uv.lock ./
COPY packages/domo-speech/pyproject.toml packages/domo-speech/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --no-install-workspace --package domo-speech $SPEECH_EXTRAS

COPY packages/domo-speech/src packages/domo-speech/src
COPY packages/domo-speech/README.md packages/domo-speech/README.md
# A graphics card also needs the CUDA 12 libraries (cuBLAS and cuDNN 9: the extra `gpu`, covered
# by the lock file like the rest), which the driver the container toolkit hands in does not
# include; compose.gpu.yaml adds the extra. A plain processor needs none.
RUN uv sync --frozen --no-dev --no-editable --package domo-speech $SPEECH_EXTRAS

# --- Runtime ---------------------------------------------------------------------------------
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# An unprivileged user, and the models directory it owns: a new named volume mounted on /models
# takes this ownership.
RUN groupadd --system --gid 10001 domo \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin domo \
    && install -d -o 10001 -g 10001 -m 0700 /models

COPY --from=build /app/.venv /app/.venv
COPY packages/domo-speech/NOTICE /app/NOTICE
COPY packages/domo-speech/healthcheck.py /app/healthcheck.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORT_DISABLE_TELEMETRY=1 \
    DOMO_SPEECH_HOST=0.0.0.0 \
    DOMO_SPEECH_MODEL_DIR=/models

USER 10001:10001
WORKDIR /app

EXPOSE 8083

# Healthy when ready, or preparing with no failure; a service that failed (any reason) is not.
# compose runs the same script.
HEALTHCHECK --interval=10s --timeout=5s --start-period=600s --start-interval=5s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["domo-speech"]
