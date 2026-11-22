"""The isolation of the stack: networks, published ports, container hardening, and the volumes
that carry the instance's secrets."""

import ipaddress
import json
import socket
import uuid
from collections.abc import Iterator

import pytest

from .stack_support import StackInfo, run

pytestmark = pytest.mark.stack

# A port the host listener below uses; nothing of the stack listens on it.
HOST_PROBE_PORT = 18999

CONNECT = (
    "import socket, sys\n"
    "socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4).close()\n"
)


def test_core_has_no_internet(stack):
    r = stack.exec(
        "core",
        ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"],
    )
    assert r.returncode != 0


def test_core_cannot_resolve_external_names(stack):
    r = stack.exec(
        "core", ["python", "-c", "import socket; socket.getaddrinfo('example.com', 443)"]
    )
    assert r.returncode != 0


def test_network_attachments(stack):
    def nets(s):
        return set(stack.inspect(s)["NetworkSettings"]["Networks"])

    p = stack.project
    assert nets("core") == {f"{p}_core_net"} and nets("db") == {f"{p}_core_net"}
    assert nets("gatekeeper") == {f"{p}_core_net", f"{p}_egress"}
    assert nets("edge") == {f"{p}_core_net", f"{p}_lan"}


def test_only_edge_publishes_ports(stack):
    for s in ("core", "gatekeeper", "db"):
        ports = stack.inspect(s)["NetworkSettings"]["Ports"] or {}
        assert all(not bindings for bindings in ports.values())


def test_containers_hardened(stack):
    for s in ("core", "gatekeeper"):
        host = stack.inspect(s)["HostConfig"]
        assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]


# --- beyond the brief's list ------------------------------------------------------------------


def test_the_core_network_is_internal_and_its_gateway_isolated(stack):
    network = stack.inspect_network("core_net")
    assert network["Internal"] is True
    assert network["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"


def test_the_core_and_the_gatekeeper_run_as_an_unprivileged_user(stack):
    for s in ("core", "gatekeeper"):
        assert stack.inspect(s)["Config"]["User"] == "10001:10001"
        assert stack.exec(s, ["id", "-u"]).stdout.strip() == "10001"


def capabilities(names):
    """Capability names without the ``CAP_`` prefix (Docker 29 adds it, older versions do not)."""
    return {name.removeprefix("CAP_") for name in names or ()}


def test_the_edge_keeps_only_the_capability_to_bind_ports(stack):
    host = stack.inspect("edge")["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert capabilities(host["CapAdd"]) == {"NET_BIND_SERVICE"}
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["ReadonlyRootfs"]


def test_init_ran_once_with_only_the_capability_to_give_files_away(stack):
    container = stack.inspect("init")
    assert container["State"]["Status"] == "exited"
    assert container["State"]["ExitCode"] == 0
    host = container["HostConfig"]
    assert host["CapDrop"] == ["ALL"] and capabilities(host["CapAdd"]) == {"CHOWN"}
    assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none"
    assert "no-new-privileges:true" in host["SecurityOpt"]


def test_every_long_running_service_is_healthy_and_restarts(stack):
    for s in ("edge", "core", "gatekeeper", "db", "sandbox"):
        container = stack.inspect(s)
        assert container["State"]["Health"]["Status"] == "healthy", s
        assert container["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", s


def test_each_volume_is_mounted_only_where_it_is_needed(stack):
    def mounts(s):
        return {
            (m["Name"] if m["Type"] == "volume" else m["Destination"], m["RW"])
            for m in stack.inspect(s)["Mounts"]
        }

    v = stack.network_name  # volumes are named like networks: <project>_<name>
    assert mounts("init") == {(v("db-auth"), True), (v("core-auth"), True)}
    assert mounts("db") == {(v("db-data"), True), (v("db-auth"), False)}
    assert mounts("core") == {
        (v("db-auth"), False),
        (v("core-auth"), False),
        (v("sandbox-socket"), False),
    }
    assert mounts("gatekeeper") == {(v("vault-data"), True), (v("core-auth"), False)}
    assert mounts("sandbox") == {(v("sandbox-socket"), True)}
    assert mounts("edge") == {
        (v("caddy-data"), True),
        (v("caddy-config"), True),
        ("/etc/caddy/Caddyfile", False),
        ("/etc/caddy/start.sh", False),
    }


STAT_FILE = (
    "import os, sys\n"
    "s = os.stat(sys.argv[1])\n"
    "print(s.st_uid, s.st_gid, oct(s.st_mode & 0o777), s.st_size)\n"
)


def test_the_secret_files_belong_to_their_readers(stack):
    password = stack.exec("core", ["python", "-c", STAT_FILE, "/run/domo/db-auth/db_password"])
    token = stack.exec("core", ["python", "-c", STAT_FILE, "/run/domo/core-auth/core_token"])
    uid, gid, mode, size = password.stdout.split()
    assert (uid, gid, mode) == ("10001", "999", oct(0o440)) and int(size) >= 40
    uid, gid, mode, size = token.stdout.split()
    assert (uid, gid, mode) == ("10001", "10001", oct(0o400)) and int(size) >= 40


def test_each_reader_can_read_its_secret(stack):
    read = "import sys; assert len(open(sys.argv[1]).read()) >= 40"
    password, token = "/run/domo/db-auth/db_password", "/run/domo/core-auth/core_token"
    for service, path in (("core", password), ("core", token), ("gatekeeper", token)):
        assert stack.exec(service, ["python", "-c", read, path]).returncode == 0, (service, path)
    assert stack.exec("db", ["test", "-r", password], user="postgres").returncode == 0


def test_the_core_trusts_the_forwarded_headers_of_the_edge_alone(stack):
    edge = stack.inspect("edge")["NetworkSettings"]["Networks"][stack.network_name("core_net")]
    core_env = dict(e.split("=", 1) for e in stack.inspect("core")["Config"]["Env"])
    assert core_env["DOMO_TRUSTED_PROXIES"] == edge["IPAddress"]


def test_the_gatekeeper_alone_has_a_way_out(stack):
    """The control of the core's test: from the same machine, the gatekeeper does reach out."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    except OSError:
        pytest.skip("this machine has no Internet access")
    assert stack.exec("gatekeeper", ["python", "-c", CONNECT, "1.1.1.1", "443"]).returncode == 0


@pytest.fixture
def host_listener(stack: StackInfo) -> Iterator[None]:
    """A web server in the host's network namespace (the Docker VM's, on Docker Desktop),
    listening on every address, from the core's image."""
    image = stack.inspect("core")["Image"]
    name = f"{stack.project}-host-listener-{uuid.uuid4().hex[:8]}"
    command = ["docker", "run", "--detach", "--rm", "--name", name, "--network", "host"]
    command += ["--no-healthcheck", image, "python", "-m", "http.server", str(HOST_PROBE_PORT)]
    started = run(command, env=stack.env)
    assert started.returncode == 0, started.stderr
    try:
        yield
    finally:
        run(["docker", "rm", "--force", name], env=stack.env)


def test_core_cannot_reach_the_host_through_its_network(stack, host_listener):
    """Docker 28's isolated gateway: on an internal network a container could still reach the
    host's own services through the bridge's address; with ``gateway_mode_ipv4=isolated`` the
    bridge has no address on that network."""
    lan_gateway = stack.inspect_network("lan")["IPAM"]["Config"][0]["Gateway"]
    # The control: from an ordinary bridge network, the listener is reachable at the gateway.
    reached = stack.exec(
        "edge",
        ["wget", "-q", "-O", "/dev/null", "-T", "5", f"http://{lan_gateway}:{HOST_PROBE_PORT}/"],
    )
    assert reached.returncode == 0, reached.stderr
    # Every address the bridge could have on core_net: the gateway Docker reports (none when it
    # is isolated), else the first address of the subnet, or of its dynamic range (Docker's
    # choice when a range is set: <prefix>.128). And the host's address on another network.
    # Isolated, the network reserves no gateway, so a container may hold one of these addresses
    # (<prefix>.128 is then usually the gatekeeper's): nothing of the stack listens on the
    # probe's port, so only the host could answer.
    [ipam] = stack.inspect_network("core_net")["IPAM"]["Config"]
    candidates = {lan_gateway, *([ipam["Gateway"]] if ipam.get("Gateway") else [])}
    candidates.add(str(next(ipaddress.ip_network(ipam["Subnet"]).hosts())))
    if ipam.get("IPRange"):
        candidates.add(str(ipaddress.ip_network(ipam["IPRange"]).network_address))
    for address in sorted(candidates):
        attempt = stack.exec("core", ["python", "-c", CONNECT, address, str(HOST_PROBE_PORT)])
        assert attempt.returncode != 0, f"the core reached the host at {address}"


# --- the sandbox -----------------------------------------------------------------------------

# Run inside the sandbox's own interpreter (nothing else is in the image): each target must fail
# to resolve or to connect.
SANDBOX_REACH = (
    "import socket, sys\n"
    "for target in sys.argv[1:]:\n"
    "    host, port = target.split(':')\n"
    "    try:\n"
    "        socket.create_connection((host, int(port)), timeout=3).close()\n"
    "    except OSError as error:\n"
    "        print('unreachable', target, type(error).__name__)\n"
    "    else:\n"
    "        print('REACHED', target)\n"
)


def test_the_sandbox_reaches_nothing(stack):
    """No network at all: the other services, by name, the outside, and the host's addresses."""
    targets = ["gatekeeper:8000", "db:5432", "core:8000", "edge:443", "1.1.1.1:443"]
    targets += ["example.com:443", "127.0.0.1:8000"]
    result = stack.exec("sandbox", ["python", "-c", SANDBOX_REACH, *targets])
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(targets) and all(line.startswith("unreachable") for line in lines)
    resolving = stack.exec(
        "sandbox", ["python", "-c", "import socket; socket.getaddrinfo('gatekeeper', 80)"]
    )
    assert resolving.returncode != 0
    interfaces = stack.exec(
        "sandbox", ["python", "-c", "import os; print(os.listdir('/sys/class/net'))"]
    )
    assert interfaces.stdout.strip() == "['lo']"


def test_the_sandbox_is_not_on_any_network_and_keeps_its_limits(stack):
    container = stack.inspect("sandbox")
    host = container["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert set(container["NetworkSettings"]["Networks"]) == {"none"}
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    assert capabilities(host["CapAdd"]) == {"SETUID", "SETGID"}
    assert host["Init"] is True
    assert host["Memory"] > 0 and host["MemorySwap"] == host["Memory"] and host["PidsLimit"] > 0
    assert container["State"]["Health"]["Status"] == "healthy"


def test_the_core_reaches_the_launcher_through_the_socket(stack):
    code = (
        "import httpx\n"
        "transport = httpx.HTTPTransport(uds='/run/domo/sandbox/launcher.sock')\n"
        "client = httpx.Client(transport=transport)\n"
        "r = client.get('http://sandbox/health')\n"
        "print(r.status_code)\n"
    )
    result = stack.exec("core", ["python", "-c", code])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200"


# What a run tries from inside the container: to write in every place it could. The code is
# sent through the socket, from the core's container, exactly as the core will.
WRITE_ATTEMPTS = """
def run(entry):
    import os
    out = {"uid": os.getuid()}
    for path in ("/tmp/x", "/run/domo/sandbox/x", "/app/x", "/x", "/dev/shm/x"):
        try:
            open(path, "w").close()
            out[path] = "written"
        except OSError as error:
            out[path] = type(error).__name__
    return out
"""
# A run over its own channel, led by the core's client as the core leads one (the launcher's
# old route without a channel is gone), every request of the program refused.
RUN_THROUGH_THE_SOCKET = (
    "import asyncio, json, sys\n"
    "from pathlib import Path\n"
    "from domo_common.sandbox_api import RunRequest\n"
    "from domo_core.sandbox_client import REFUSE_ALL, SandboxClient, new_http_client\n"
    "async def main():\n"
    "    http = new_http_client(Path('/run/domo/sandbox/launcher.sock'))\n"
    "    request = RunRequest(code=sys.argv[1], entry={}, max_seconds=20)\n"
    "    result = await SandboxClient(http).run(request, answer=REFUSE_ALL)\n"
    "    await http.aclose()\n"
    "    print(json.dumps(result.model_dump(mode='json')))\n"
    "asyncio.run(main())\n"
)


def test_a_run_has_its_own_account_and_keeps_no_file(stack):
    result = stack.exec("core", ["python", "-c", RUN_THROUGH_THE_SOCKET, WRITE_ATTEMPTS])
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["status"] == "done", answer
    output = answer["output"]
    assert 20000 <= output.pop("uid") <= 29999
    assert set(output.values()) <= {"PermissionError", "OSError", "FileNotFoundError"}, output
    assert "written" not in output.values()
    # Nothing was left behind, in the places the launcher itself can write.
    listing = stack.exec(
        "sandbox",
        ["python", "-c", "import os; print(os.listdir('/tmp'), os.listdir('/run/domo/sandbox'))"],
    )
    assert listing.stdout.strip() == "[] ['launcher.sock']"
