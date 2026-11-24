"""Where the fake device of the host is published (``host_device_bind``), with the docker calls
faked: no engine is needed, so these tests are not marked ``stack``.

The gatekeeper reaches that device as the host, ``host.docker.internal``. Docker Desktop forwards
the name to the host's loopback; a plain Linux engine maps it to its bridge address (the
host-gateway), where a port published on 127.0.0.1 is not reachable. The narrowest address that
works is chosen, never every address (that would show the fake device to the whole network)."""

import subprocess
from collections.abc import Sequence

import pytest

from .stack_support import PROBE_IMAGE, host_device_bind

LINUX_GATEWAY = "172.17.0.1"
# /etc/hosts as the engine writes it with --add-host (only the name's IPv4 line counts).
HOSTS_LINUX = (
    "127.0.0.1\tlocalhost\n"
    "::1\tlocalhost ip6-localhost ip6-loopback\n"
    "# a comment host.docker.internal\n"
    "10.0.0.9\tsomething-else\n"
    "fdc4:f303:9324::254\thost.docker.internal\n"
    f"{LINUX_GATEWAY}\thost.docker.internal\n"
)


class FakeDocker:
    """Answers ``docker info`` and ``docker run`` as an engine would, and keeps the commands."""

    def __init__(
        self, os_name: str, *, hosts: str = HOSTS_LINUX, run_code: int = 0, info_code: int = 0
    ) -> None:
        self.os_name, self.hosts = os_name, hosts
        self.run_code, self.info_code = run_code, info_code
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.commands.append(command)
        if command[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(command, self.info_code, self.os_name + "\n", "")
        if command[:2] == ["docker", "run"]:
            out = self.hosts if self.run_code == 0 else ""
            return subprocess.CompletedProcess(command, self.run_code, out, "no such image")
        raise AssertionError(f"unexpected command {command}")


def test_on_docker_desktop_the_device_stays_on_the_loopback():
    docker = FakeDocker("Docker Desktop")
    assert host_device_bind(docker) == "127.0.0.1"
    assert [c[:2] for c in docker.commands] == [["docker", "info"]]


@pytest.mark.parametrize("os_name", ["Ubuntu 24.04.1 LTS", "Alpine Linux v3.20", "Debian"])
def test_on_any_other_engine_the_device_is_on_the_host_gateway(os_name):
    """Asked of the engine in a container without a network: a daemon with no default bridge
    answers too."""
    docker = FakeDocker(os_name)
    assert host_device_bind(docker) == LINUX_GATEWAY
    [_, probe] = docker.commands
    assert probe[:3] == ["docker", "run", "--rm"]
    assert probe[probe.index("--network") + 1] == "none"
    assert probe[probe.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert PROBE_IMAGE in probe
    assert probe[-2:] == ["cat", "/etc/hosts"]


def test_the_probe_image_is_one_the_stack_already_uses():
    assert PROBE_IMAGE.startswith("python:3.12-slim@sha256:")


@pytest.mark.parametrize(
    ("docker", "says"),
    [
        (FakeDocker("Ubuntu", run_code=125), "host-gateway"),
        (FakeDocker("Ubuntu", hosts=""), "host-gateway"),
        (FakeDocker("Ubuntu", hosts="not-an-address host.docker.internal\n"), "host-gateway"),
        (FakeDocker("Ubuntu", hosts="fdc4::254 host.docker.internal\n"), "host-gateway"),
        (FakeDocker("Ubuntu", hosts="172.17.0.1 another-name\n"), "host-gateway"),
        (FakeDocker("Ubuntu", hosts="0.0.0.0 host.docker.internal\n"), "0.0.0.0"),
        (FakeDocker("", info_code=1), "docker info"),
    ],
)
def test_an_unknown_gateway_fails_loudly_and_never_falls_back_to_every_address(docker, says):
    with pytest.raises(pytest.fail.Exception, match=says):
        host_device_bind(docker)
