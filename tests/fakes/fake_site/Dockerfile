# The fake site of the stack tests of going out (see app.py and tests/stack/compose.going-out.yaml):
# never part of an installation. Its certificate and key are mounted at /certs by the tests.
#
# The base image is the gatekeeper's own, pinned by digest (the tag is kept for readers), as the
# fake model's. The service uses the standard library only: nothing is installed.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# An unprivileged user, with no home and no shell.
RUN groupadd --system --gid 10001 fakesite \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin fakesite

COPY app.py /app/app.py

# No bytecode: the root filesystem is read-only when the container runs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /app

EXPOSE 8443

# The health check does not check the certificate: it only asks whether the site answers.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import ssl, urllib.request as u; u.build_opener(u.ProxyHandler({}), u.HTTPSHandler(context=ssl._create_unverified_context())).open('https://127.0.0.1:8443/health', timeout=2)"]

# The account to expect comes from the environment (FAKE_SITE_USER, FAKE_SITE_PASSWORD); without
# it the service refuses to start.
CMD ["python", "/app/app.py"]
