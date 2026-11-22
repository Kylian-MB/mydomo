# The fake model service of the end-to-end tests (see app.py and compose.test.yaml): never part of
# an installation.
#
# Build from this directory:
#   docker build tests/fakes/fake_llm
#
# The base image is the gatekeeper's own, pinned by digest (the tag is kept for readers). To
# update the pin, look up the digest of the tag with `docker buildx imagetools inspect
# python:3.12-slim`. The service uses the standard library only: nothing is installed.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# An unprivileged user, with no home and no shell.
RUN groupadd --system --gid 10001 fakellm \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin fakellm

COPY app.py /app/app.py

# No bytecode: the root filesystem is read-only when the container runs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /app

EXPOSE 9000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:9000/health', timeout=2)"]

# The key to expect comes from the environment (FAKE_LLM_EXPECTED_KEY); without it the service
# refuses to start.
CMD ["python", "/app/app.py"]
