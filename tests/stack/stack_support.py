"""Starting the Docker Compose stack for the tests, and looking into it.

``running_stack`` builds the images from the working tree, starts the stack, waits until every
service is healthy and the HTTPS site answers, and always takes the stack down again with its
volumes: when the tests are over, when they fail, and when the start itself fails. It also takes
down what a previous, interrupted run may have left, so every run starts from a new instance.

``StackInfo`` is what the tests get: the URLs of the page, and ways to run a command in a
service, read its log and inspect its container.

The project name (``domo-test``), and the ports (18443 and 18080, on 127.0.0.1) and internal
network (``DOMO_CORE_NET_PREFIX=10.247.91``, so ``10.247.91.0/24``) of ``test.env``, keep the
test stack apart from a real instance on the same machine: its own containers, networks, volumes
and images. Docker refuses a network whose fixed subnet overlaps another's, so a variant of the
stack (more compose files, more environment) that may run at the same time as another stack
needs its own project name, its own ports and its own ``DOMO_CORE_NET_PREFIX`` (through its
env file or ``extra_env``: the shell's ``DOMO_*`` variables are not passed on).

``running_stack_with_fake_llm`` is the variant of the end-to-end tests (``tests/e2e``): the stack
plus the fake model service of ``compose.test.yaml``, and a new random test key it accepts
(``StackInfo.test_key``). Those stacks have their own project names, ports and core networks
(``tests/e2e/*.env``), so that they can run next to the one of ``test.env``.

The environment the ``docker`` commands run in is the test runner's, without its ``DOMO_*``
variables and the ones ``test.env`` sets: a variable of the shell would win over ``test.env``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Relative to the repository root, where the docker commands run.
TEST_ENV_FILE = "tests/stack/test.env"
BASE_COMPOSE_FILES: tuple[str, ...] = ("compose.yaml",)
# The end-to-end tests add a fake model provider to the stack (see compose.test.yaml); the key it
# accepts is passed to Compose through this variable.
FAKE_LLM_COMPOSE_FILES: tuple[str, ...] = (*BASE_COMPOSE_FILES, "compose.test.yaml")
FAKE_LLM_KEY_VARIABLE = "FAKE_LLM_EXPECTED_KEY"
# The tests of going out add a device twice (on another machine, and on a port of this one) and a
# site with a login (see tests/stack/compose.going-out.yaml), in a stack of their own.
GOING_OUT_FAKES = "tests/stack/compose.going-out.yaml"
GOING_OUT_COMPOSE_FILES: tuple[str, ...] = (*BASE_COMPOSE_FILES, GOING_OUT_FAKES)
GOING_OUT_ENV_FILE = "tests/stack/going_out.env"
GOING_OUT_PROJECT = "domo-test-out"
# The port of this machine the device on the host is published on.
GOING_OUT_HOST_DEVICE_PORT = 18996
# The address of this machine it is published on (see host_device_bind).
GOING_OUT_HOST_DEVICE_BIND = "GOING_OUT_HOST_DEVICE_BIND"
HOST_NAME_IN_CONTAINERS = "host.docker.internal"
# The image the stack's own services and fakes are built from: already there, and it has getent.
PROBE_IMAGE = next(
    line.split()[1]
    for line in (REPO_ROOT / "tests/fakes/fake_device/Dockerfile")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.startswith("FROM ")
)
PROJECT = "domo-test"
MIN_DOCKER_MAJOR = 28

# The first build downloads the dependencies: give it time.
UP_TIMEOUT_S = 900
# After `up --wait`: until the edge has issued its certificate and the page answers.
READY_TIMEOUT_S = 60
COMMAND_TIMEOUT_S = 120
# The core logs its setup code when it starts; logs can lag a little behind the container.
SETUP_CODE_WAIT_S = 15
SETUP_CODE_LINE = re.compile(
    r"The assistant setup code: "
    r"([A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){2})"
)


def read_env_file(path: Path) -> dict[str, str]:
    """The ``NAME=value`` lines of an env file (comments and blank lines skipped)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def docker_environment(
    env_file: str = TEST_ENV_FILE, extra_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment of the docker commands: the runner's, minus what could override the
    settings of ``env_file``, plus ``extra_env``."""
    overridden = set(read_env_file(REPO_ROOT / env_file))
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DOMO_", "FAKE_LLM_")) and name not in overridden
    }
    env.update(extra_env or {})
    return env


def run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` from the repository root and capture its output (never raises on a
    non-zero exit: callers look at ``returncode``)."""
    return subprocess.run(
        list(command),
        cwd=REPO_ROOT,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


@dataclass(frozen=True)
class StackInfo:
    """A running stack, as the tests see it."""

    base_url: str
    http_url: str
    project: str
    compose_command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])

    def compose(
        self, *args: str, timeout: float = COMMAND_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        """``docker compose`` with this stack's project, env file and compose files."""
        return run([*self.compose_command, *args], env=self.env, timeout=timeout)

    def exec(
        self, service: str, cmd: list[str], *, user: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd`` in the running container of ``service`` (as ``user`` when given)."""
        options = ["-T", *(["--user", user] if user is not None else [])]
        return self.compose("exec", *options, service, *cmd)

    def logs(self, service: str) -> str:
        """Everything ``service`` wrote to its output so far. A failed ``docker compose logs`` is
        an error, not an empty log: a test that looks for something to be absent must not pass on
        a log it could not read."""
        result = self.compose("logs", "--no-color", "--no-log-prefix", service)
        if result.returncode != 0:
            raise AssertionError(f"`docker compose logs {service}` failed: {result.stderr}")
        return result.stdout

    def setup_code(self) -> str:
        """The one-time setup code the core logged when it started (the last one, if it logged
        several): what whoever runs the server reads in the logs and types into the first page."""
        deadline = time.monotonic() + SETUP_CODE_WAIT_S
        while True:
            codes = SETUP_CODE_LINE.findall(self.logs("core"))
            if codes:
                return codes[-1]
            if time.monotonic() > deadline:
                raise AssertionError("the core's log has no setup code")
            time.sleep(0.5)

    @property
    def test_key(self) -> str:
        """The API key the fake model service accepts (stacks started with it only)."""
        try:
            return self.env[FAKE_LLM_KEY_VARIABLE]
        except KeyError:
            raise AssertionError("this stack has no fake model service") from None

    def container_id(self, service: str) -> str:
        """The id of the container of ``service`` (running or not: ``init`` has exited)."""
        ids = self.compose("ps", "--all", "--quiet", service).stdout.split()
        if len(ids) != 1:
            raise AssertionError(f"expected one container for {service}, found {ids}")
        return ids[0]

    def inspect(self, service: str) -> dict[str, Any]:
        """``docker inspect`` of the container of ``service``."""
        return self.inspect_object(self.container_id(service))

    def inspect_network(self, network: str) -> dict[str, Any]:
        """``docker network inspect`` of one of the compose file's networks."""
        return self.inspect_object(self.network_name(network), kind="network")

    def inspect_object(self, name: str, *, kind: str | None = None) -> dict[str, Any]:
        command = ["docker", *([kind] if kind else []), "inspect", name]
        result = run(command, env=self.env)
        if result.returncode != 0:
            raise AssertionError(f"{' '.join(command)} failed: {result.stderr}")
        [found] = json.loads(result.stdout)
        return found

    def network_name(self, network: str) -> str:
        """The Docker name of one of the compose file's networks (or volumes)."""
        return f"{self.project}_{network}"


def require_docker(env: Mapping[str, str]) -> None:
    """Fail when there is no Docker engine (or one older than 28); skip when it only runs
    Windows containers."""
    result = run(["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}"], env=env)
    if result.returncode != 0:
        pytest.fail(
            "The stack tests need a running Docker engine (28 or later, with Compose): "
            f"`docker version` failed: {result.stderr.strip()}"
        )
    version, _, server_os = result.stdout.strip().partition(" ")
    if server_os != "linux":
        pytest.skip(f"The Docker engine runs {server_os} containers; the stack needs Linux ones")
    major = int(version.split(".", 1)[0])
    if major < MIN_DOCKER_MAJOR:
        pytest.fail(f"The stack needs Docker {MIN_DOCKER_MAJOR} or later, found {version}")


def wait_until_ready(base_url: str) -> None:
    """Wait until the page answers over HTTPS (the edge issues its certificate when it starts)."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error = "no answer"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/ca.crt", verify=False, timeout=5)
            if response.status_code == 200:
                return
            last_error = f"status {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = repr(exc)
        time.sleep(1)
    pytest.fail(f"The page at {base_url} did not answer in {READY_TIMEOUT_S} s: {last_error}")


@contextmanager
def running_stack(
    *,
    files: Sequence[str] = BASE_COMPOSE_FILES,
    extra_env: Mapping[str, str] | None = None,
    project: str = PROJECT,
    env_file: str = TEST_ENV_FILE,
) -> Iterator[StackInfo]:
    """Start the stack (``docker compose up -d --build --wait``), yield it, and take it down with
    its volumes (``down -v --remove-orphans``) whatever happens.

    Two stacks running at the same time need different ``project`` names, ports and
    ``DOMO_CORE_NET_PREFIX`` values (see the module's docstring).
    """
    env = docker_environment(env_file, extra_env)
    require_docker(env)
    settings = read_env_file(REPO_ROOT / env_file)
    compose = ["docker", "compose", "-p", project, "--env-file", env_file]
    for compose_file in files:
        compose += ["-f", compose_file]
    info = StackInfo(
        base_url=f"https://localhost:{settings['DOMO_HTTPS_PORT']}",
        http_url=f"http://localhost:{settings['DOMO_HTTP_PORT']}",
        project=project,
        compose_command=tuple(compose),
        env=env,
    )

    def down() -> subprocess.CompletedProcess[str]:
        return info.compose("down", "--volumes", "--remove-orphans", timeout=COMMAND_TIMEOUT_S)

    down()  # what an interrupted run may have left
    try:
        started = info.compose("up", "--detach", "--build", "--wait", timeout=UP_TIMEOUT_S)
        if started.returncode != 0:
            status = info.compose("ps", "--all").stdout
            logs = info.compose("logs", "--no-color", "--tail", "40").stdout
            pytest.fail(
                f"`docker compose up` failed ({started.returncode}).\n{started.stderr[-4000:]}\n"
                f"--- containers ---\n{status}\n--- logs ---\n{logs[-8000:]}"
            )
        wait_until_ready(info.base_url)
        yield info
    finally:
        stopped = down()
    # Only reached when nothing else failed: a failure to clean up must not hide the first one.
    if stopped.returncode != 0:
        raise RuntimeError(f"`docker compose down` failed: {stopped.stderr}")


@contextmanager
def running_stack_with_fake_llm(*, project: str, env_file: str) -> Iterator[StackInfo]:
    """``running_stack`` with the fake model service of ``compose.test.yaml``, which accepts one
    new random test key per run (``StackInfo.test_key``). It is a variant of the stack that may run
    next to another one, so ``project`` and ``env_file`` (ports, ``DOMO_CORE_NET_PREFIX``) are
    required."""
    key = "sk-test-" + secrets.token_hex(16)
    with running_stack(
        files=FAKE_LLM_COMPOSE_FILES,
        extra_env={FAKE_LLM_KEY_VARIABLE: key},
        project=project,
        env_file=env_file,
    ) as info:
        yield info


# --- going out --------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoingOutSecrets:
    """What the fakes of going out accept, new for each run: the devices' token and the site's
    account. The tests type them into the vault's forms; nothing else of Domo is given them."""

    device_token: str
    site_user: str
    site_password: str


def write_test_authority(directory: Path, site_name: str) -> tuple[Path, Path]:
    """A certificate authority of the tests' own, and a certificate it signs for ``site_name``:
    ``site.crt`` and ``site.key`` in ``directory`` (mounted into the fake site), and
    ``ca-bundle.pem`` beside it, the usual authorities (``certifi``) plus this one (mounted into
    the gatekeeper). Returns the certificates' directory and the bundle."""
    import datetime

    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Going out tests authority")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    site_key = ec.generate_private_key(ec.SECP256R1())
    site = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, site_name)]))
        .issuer_name(ca_name)
        .public_key(site_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(site_name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    certs = directory / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "site.crt").write_bytes(site.public_bytes(serialization.Encoding.PEM))
    (certs / "site.key").write_bytes(
        site_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    bundle = directory / "ca-bundle.pem"
    usual = Path(certifi.where()).read_bytes()
    bundle.write_bytes(usual.rstrip(b"\n") + b"\n" + ca.public_bytes(serialization.Encoding.PEM))
    return certs, bundle


def host_device_bind(
    docker: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> str:
    """The address of this machine the fake device on the host is published on, so that the
    gatekeeper reaches it as ``host.docker.internal`` and nothing else of the network does.

    Docker Desktop forwards that name to this machine's loopback: 127.0.0.1. Any other engine
    maps it to its host-gateway address (the bridge's, as ``extra_hosts`` of ``compose.yaml``
    does), where a port published on 127.0.0.1 is not reachable: that address, asked of the engine
    itself through a container (the ``/etc/hosts`` it writes there). Never every address (the
    fake device would be on the network of the house): when the address cannot be known, the
    tests fail and say why. ``docker`` runs a docker command (``run`` with the environment of
    the stack, in the tests of the stack)."""
    info = docker(["docker", "info", "--format", "{{.OperatingSystem}}"])
    if info.returncode != 0:
        pytest.fail(f"`docker info` failed: {info.stderr.strip()}")
    if "Docker Desktop" in info.stdout:
        return "127.0.0.1"
    # No network: the engine writes the name into /etc/hosts all the same, so a daemon without a
    # default bridge (and with its host-gateway-ip set) answers too. getent would not: without an
    # IPv4 interface it gives nothing for the name.
    # If the daemon's host-gateway-ip is an address of the house, the fake device is published
    # there while the tests run (it answers only with the run's token).
    probe = docker(
        [
            "docker", "run", "--rm", "--network", "none",
            "--add-host", f"{HOST_NAME_IN_CONTAINERS}:host-gateway",
            PROBE_IMAGE, "cat", "/etc/hosts",
        ]
    )  # fmt: skip
    found: list[ipaddress.IPv4Address] = []
    for line in probe.stdout.splitlines() if probe.returncode == 0 else ():
        address, *names = line.split("#", 1)[0].split() or [""]
        if HOST_NAME_IN_CONTAINERS not in names:
            continue
        try:
            found.append(ipaddress.IPv4Address(address))
        except ValueError:
            continue
    if not found:
        pytest.fail(
            f"The engine ({info.stdout.strip()}) gave no IPv4 host-gateway address for "
            f"{HOST_NAME_IN_CONTAINERS} (exit {probe.returncode}: {probe.stderr.strip()[-500:]}); "
            "the fake device on the host cannot be published where the gatekeeper reaches it"
        )
    if found[0].is_unspecified:
        pytest.fail(
            f"The engine maps {HOST_NAME_IN_CONTAINERS} to {found[0]}: the fake device would be "
            "published on every address of this machine"
        )
    return str(found[0])


@contextmanager
def running_stack_going_out(
    directory: Path,
    site_name: str,
    *,
    files: Sequence[str] = GOING_OUT_COMPOSE_FILES,
    project: str = GOING_OUT_PROJECT,
    env_file: str = GOING_OUT_ENV_FILE,
    host_device_port: int = GOING_OUT_HOST_DEVICE_PORT,
    extra_env: Mapping[str, str] | None = None,
) -> Iterator[GoingOutStack]:
    """``running_stack`` with the fakes of going out (``compose.going-out.yaml``), in a stack of
    its own (``going_out.env``), with new ``GoingOutSecrets`` (``StackInfo.going_out``) and the
    tests' own authority written in ``directory``. The end-to-end tests run the same fakes in a
    stack of theirs, with the fake model: other ``files`` (``GOING_OUT_FAKES`` among them), another
    ``project``, ``env_file`` and ``host_device_port``, and ``extra_env`` (the fake model's key)."""
    certs, bundle = write_test_authority(directory, site_name)
    env = docker_environment(env_file, extra_env)
    require_docker(env)
    bind = host_device_bind(lambda command: run(command, env=env))
    given = GoingOutSecrets(
        device_token="dt-" + secrets.token_hex(16),
        site_user="alex-" + secrets.token_hex(4),
        site_password="pw-" + secrets.token_hex(16),
    )
    with running_stack(
        files=files,
        extra_env={
            **(extra_env or {}),
            "FAKE_DEVICE_TOKEN": given.device_token,
            "FAKE_SITE_USER": given.site_user,
            "FAKE_SITE_PASSWORD": given.site_password,
            "GOING_OUT_CERTS_DIR": str(certs),
            "GOING_OUT_CA_BUNDLE": str(bundle),
            "GOING_OUT_HOST_DEVICE_PORT": str(host_device_port),
            GOING_OUT_HOST_DEVICE_BIND: bind,
        },
        project=project,
        env_file=env_file,
    ) as info:
        yield GoingOutStack(**vars(info), going_out=given)


@dataclass(frozen=True)
class GoingOutStack(StackInfo):
    """The stack of going out, with what its fakes accept."""

    going_out: GoingOutSecrets | None = None
