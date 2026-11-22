"""What ``compose.yaml`` resolves to, with and without settings (``docker compose config``, no
container started)."""

import ipaddress
import json
from pathlib import Path
from typing import Any

import pytest

from .stack_support import (
    BASE_COMPOSE_FILES,
    FAKE_LLM_COMPOSE_FILES,
    REPO_ROOT,
    TEST_ENV_FILE,
    docker_environment,
    read_env_file,
    run,
)
from .test_https import BY_IP_HTTP_PORT, BY_IP_HTTPS_PORT, ECHO_HTTP_PORT, ECHO_HTTPS_PORT
from .test_network import HOST_PROBE_PORT

pytestmark = pytest.mark.stack

# The env files of the end-to-end tests' stacks (see tests/e2e/conftest.py).
E2E_ENV_FILES = ("tests/e2e/shared.env", "tests/e2e/fresh.env")


def resolved(
    env_file: Path,
    files: tuple[str, ...] = BASE_COMPOSE_FILES,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The compose files as Compose resolves them with ``env_file`` (and nothing from the shell:
    neither ``DOMO_*`` nor ``TZ``), plus ``extra_env``."""
    command = ["docker", "compose", "-p", "domo-config", "--env-file", str(env_file)]
    for compose_file in files:
        command += ["-f", compose_file]
    command += ["config", "--format", "json"]
    result = run(command, env=docker_environment(extra_env=extra_env))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def published(service: dict[str, Any]) -> set[tuple[str, str, int]]:
    return {(p["host_ip"], p["published"], p["target"]) for p in service.get("ports", [])}


def test_without_settings_the_page_answers_on_localhost_8443(tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    config = resolved(empty)
    edge = config["services"]["edge"]
    assert edge["environment"]["DOMO_SITE_ADDRESSES"] == "localhost"
    assert edge["environment"]["DOMO_HTTPS_PORT"] == "8443"
    assert edge["environment"]["DOMO_HTTP_PORT"] == "8080"
    assert published(edge) == {("0.0.0.0", "8443", 8443), ("0.0.0.0", "8080", 8080)}
    for name, service in config["services"].items():
        assert service["environment"]["TZ"] == "UTC", name


def test_the_example_settings_reach_the_edge():
    config = resolved(REPO_ROOT / ".env.example")
    edge = config["services"]["edge"]
    assert edge["environment"]["DOMO_SITE_ADDRESSES"] == "localhost, 192.168.1.10"
    assert published(edge) == {("0.0.0.0", "8443", 8443), ("0.0.0.0", "8080", 8080)}


@pytest.mark.parametrize("settings", ["empty", "example"])
def test_the_core_knows_the_site_addresses_of_the_edge(tmp_path, settings):
    """Passkeys only work on the host names among the site addresses (task 12): the core reads
    the same list as the edge."""
    env_file = REPO_ROOT / ".env.example"
    if settings == "empty":
        env_file = tmp_path / "empty.env"
        env_file.write_text("", encoding="utf-8")
    services = resolved(env_file)["services"]
    addresses = services["edge"]["environment"]["DOMO_SITE_ADDRESSES"]
    assert services["core"]["environment"]["DOMO_SITE_ADDRESSES"] == addresses


@pytest.mark.parametrize(("settings", "port"), [("", "8443"), ("DOMO_HTTPS_PORT=443\n", "443")])
def test_the_core_knows_the_https_port_of_the_edge(tmp_path, settings, port):
    """The origin a passkey signs carries the page's port: the core takes it from its settings,
    never from the request (task 12)."""
    env_file = tmp_path / "custom.env"
    env_file.write_text(settings, encoding="utf-8")
    services = resolved(env_file)["services"]
    assert services["edge"]["environment"]["DOMO_HTTPS_PORT"] == port
    assert services["core"]["environment"]["DOMO_HTTPS_PORT"] == port


def test_custom_ports_are_the_same_inside_and_outside(tmp_path):
    custom = tmp_path / "custom.env"
    custom.write_text(
        "DOMO_HTTPS_PORT=443\nDOMO_HTTP_PORT=80\nDOMO_BIND_ADDRESS=192.168.1.10\n",
        encoding="utf-8",
    )
    edge = resolved(custom)["services"]["edge"]
    assert published(edge) == {("192.168.1.10", "443", 443), ("192.168.1.10", "80", 80)}


def test_only_the_edge_publishes_ports(tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    services = resolved(empty)["services"]
    assert [name for name, service in services.items() if service.get("ports")] == ["edge"]


def test_the_core_trusts_the_edge_address_on_the_core_network(tmp_path):
    custom = tmp_path / "custom.env"
    custom.write_text("DOMO_CORE_NET_PREFIX=10.99.7\n", encoding="utf-8")
    config = resolved(custom)
    core_net = config["networks"]["core_net"]
    assert core_net["ipam"]["config"] == [{"subnet": "10.99.7.0/24", "ip_range": "10.99.7.128/25"}]
    edge_address = config["services"]["edge"]["networks"]["core_net"]["ipv4_address"]
    assert edge_address == "10.99.7.2"
    assert config["services"]["core"]["environment"]["DOMO_TRUSTED_PROXIES"] == edge_address


def test_the_test_stack_does_not_claim_the_core_network_of_a_real_instance(tmp_path):
    """Docker refuses a second network whose fixed subnet overlaps another's: with the same
    subnet, the test stack could not start next to a real instance (nor the other way round)."""
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")

    def core_net(config: dict[str, Any]) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        return ipaddress.ip_network(config["networks"]["core_net"]["ipam"]["config"][0]["subnet"])

    default, test = resolved(empty), resolved(REPO_ROOT / TEST_ENV_FILE)
    assert not core_net(default).overlaps(core_net(test))
    # The test stack's core still trusts its own edge.
    edge_address = test["services"]["edge"]["networks"]["core_net"]["ipv4_address"]
    assert ipaddress.ip_address(edge_address) in core_net(test)
    assert test["services"]["core"]["environment"]["DOMO_TRUSTED_PROXIES"] == edge_address


# --- compose.test.yaml: the fake model service of the end-to-end tests ----------------------------

FAKE_LLM_ENV = {"FAKE_LLM_EXPECTED_KEY": "sk-test-not-a-real-key"}


def test_the_test_file_only_adds_the_fake_model_service():
    """Nothing of the real stack changes when the test file is added: not a setting of a
    service, not a network, not a volume."""
    base = resolved(REPO_ROOT / TEST_ENV_FILE)
    with_fake = resolved(REPO_ROOT / TEST_ENV_FILE, FAKE_LLM_COMPOSE_FILES, FAKE_LLM_ENV)
    services = dict(with_fake["services"])
    fake = services.pop("fake-llm")
    assert services == base["services"]
    assert with_fake["networks"] == base["networks"]
    assert with_fake["volumes"] == base["volumes"]
    assert fake["environment"]["FAKE_LLM_EXPECTED_KEY"] == FAKE_LLM_ENV["FAKE_LLM_EXPECTED_KEY"]


def test_the_fake_model_service_is_on_the_egress_network_alone_and_hardened():
    config = resolved(REPO_ROOT / TEST_ENV_FILE, FAKE_LLM_COMPOSE_FILES, FAKE_LLM_ENV)
    fake = config["services"]["fake-llm"]
    # The way the gatekeeper reaches it (and nothing else does): no published port either.
    assert set(fake["networks"]) == {"egress"}
    assert not fake.get("ports")
    assert fake["read_only"] is True and fake["cap_drop"] == ["ALL"]
    assert fake["security_opt"] == ["no-new-privileges:true"]
    assert fake["user"] == "10001:10001"


def test_the_test_file_needs_the_key_the_fake_model_expects():
    result = run(
        ["docker", "compose", "-p", "domo-config", "--env-file", TEST_ENV_FILE]
        + [arg for name in FAKE_LLM_COMPOSE_FILES for arg in ("-f", name)]
        + ["config", "--format", "json"],
        env=docker_environment(),
    )
    assert result.returncode != 0 and "FAKE_LLM_EXPECTED_KEY" in result.stderr


def test_the_test_stacks_can_run_side_by_side():
    """The shared and the fresh stacks of the end-to-end tests run next to the stack of the other
    stack tests (a session fixture stays up until the end of the run): each one needs its own
    ports and its own core network, which Docker refuses to overlap."""
    settings = [read_env_file(REPO_ROOT / name) for name in (TEST_ENV_FILE, *E2E_ENV_FILES)]
    ports = [
        int(value) for env in settings for value in (env["DOMO_HTTPS_PORT"], env["DOMO_HTTP_PORT"])
    ]
    # And the ports other stack tests use on the host for a container of their own.
    ports += [BY_IP_HTTPS_PORT, BY_IP_HTTP_PORT, ECHO_HTTPS_PORT, ECHO_HTTP_PORT, HOST_PROBE_PORT]
    assert len(set(ports)) == len(ports)
    networks = [ipaddress.ip_network(f"{env['DOMO_CORE_NET_PREFIX']}.0/24") for env in settings]
    for index, network in enumerate(networks):
        assert not any(network.overlaps(other) for other in networks[index + 1 :])
    # And none of them is the default network of a real instance.
    default = resolved(REPO_ROOT / ".env.example")["networks"]["core_net"]["ipam"]["config"][0]
    assert not any(
        network.overlaps(ipaddress.ip_network(default["subnet"])) for network in networks
    )


# --- the sandbox ---------------------------------------------------------------------------

SECRET_VOLUMES = {"vault-data", "db-auth", "core-auth", "db-data"}


def sandbox_config() -> dict[str, Any]:
    return resolved(REPO_ROOT / TEST_ENV_FILE)["services"]["sandbox"]


def test_the_sandbox_has_no_network_at_all():
    sandbox = sandbox_config()
    assert sandbox["network_mode"] == "none"
    assert "networks" not in sandbox
    assert not sandbox.get("ports")


def test_the_sandbox_keeps_only_the_capabilities_to_change_user():
    sandbox = sandbox_config()
    assert sandbox["cap_drop"] == ["ALL"]
    assert sorted(sandbox["cap_add"]) == ["SETGID", "SETUID"]
    assert sandbox["security_opt"] == ["no-new-privileges:true"]
    assert sandbox["read_only"] is True
    assert sandbox["init"] is True
    assert sandbox["ipc"] == "none"
    [tmp] = sandbox["tmpfs"]
    assert tmp.startswith("/tmp:") and "noexec" in tmp and "size=" in tmp
    assert int(sandbox["pids_limit"]) > 0
    assert int(sandbox["mem_limit"]) > 0
    assert int(sandbox["memswap_limit"]) == int(sandbox["mem_limit"])


def volumes_of(service: dict[str, Any]) -> set[str]:
    return {m["source"] for m in service.get("volumes", []) if m["type"] == "volume"}


def test_only_core_and_the_sandbox_share_the_socket_volume():
    services = resolved(REPO_ROOT / TEST_ENV_FILE)["services"]
    sharing = {
        name for name, service in services.items() if "sandbox-socket" in volumes_of(service)
    }
    assert sharing == {"core", "sandbox"}
    socket_path = "/run/domo/sandbox"
    for name in sharing:
        targets = {
            m["target"] for m in services[name]["volumes"] if m["source"] == "sandbox-socket"
        }
        assert targets == {socket_path}
    assert services["core"]["environment"]["DOMO_SANDBOX_SOCKET"] == (
        f"{socket_path}/launcher.sock"
    )


def test_the_launcher_s_socket_is_for_the_core_s_group():
    """The launcher makes its socket root's and the core's group's (mode 0660): the group it is
    told must be the gid the core runs with, or the core could not reach it."""
    services = resolved(REPO_ROOT / TEST_ENV_FILE)["services"]
    _, core_gid = services["core"]["user"].split(":")
    assert services["sandbox"]["environment"]["DOMO_SANDBOX_CORE_GID"] == core_gid
    assert (
        services["sandbox"]["environment"]["DOMO_SANDBOX_SOCKET"]
        == (services["core"]["environment"]["DOMO_SANDBOX_SOCKET"])
    )


def test_the_sandbox_mounts_no_secret_volume():
    sandbox = sandbox_config()
    assert volumes_of(sandbox) == {"sandbox-socket"}
    assert not volumes_of(sandbox) & SECRET_VOLUMES
    assert all(m["type"] == "volume" for m in sandbox["volumes"])  # no host path either


def test_the_core_does_not_wait_for_the_sandbox():
    """The sandbox is an optional feature: the core starts and talks with people without it."""
    services = resolved(REPO_ROOT / TEST_ENV_FILE)["services"]
    assert "sandbox" not in services["core"]["depends_on"]
    assert not any("sandbox" in service.get("depends_on", {}) for service in services.values())
