import pytest
from domo_common.gatekeeper_api import Binding, HeaderLocation, ProviderRoute
from domo_gatekeeper.errors import BindingMismatchError, InsecureTransportError
from domo_gatekeeper.providers import ProviderRegistry

B = Binding(host="api.example.com", location=HeaderLocation(kind="header", name="x-api-key"))
REF = "provider:p1:api_key"

# Hosts a key may reach over plain http, as (bound host, base URL). IPv6 literals are covered by
# the cleartext_allowed tests of domo-common only: a binding cannot name one (its host has no
# colon).
LOCAL_HTTP_ROUTES = [
    ("192.168.1.10", "http://192.168.1.10:11434/v1"),
    ("127.0.0.1", "http://127.0.0.1"),
    ("localhost", "http://localhost:8080"),
    ("fake-llm", "http://fake-llm:9000/v1"),
    ("10.1.2.3", "http://10.1.2.3"),
    ("172.16.0.5", "http://172.16.0.5:8000"),
    ("169.254.1.1", "http://169.254.1.1"),
]
PUBLIC_HTTP_ROUTES = [
    ("api.example.com", "http://api.example.com"),
    ("api.example.com", "http://api.example.com:443"),
    ("api.example.com", "http://api.example.com:8080/v1"),
    ("api.example.com", "HTTP://API.example.com/v1"),
    ("eu.api.example.com", "http://eu.api.example.com"),
    ("my.internal", "http://my.internal"),
    ("8.8.8.8", "http://8.8.8.8"),
    ("172.32.0.1", "http://172.32.0.1"),
    ("100.64.0.1", "http://100.64.0.1"),
    ("192.169.0.1", "http://192.169.0.1"),
    ("134744072", "http://134744072"),  # a number, not a name: some resolvers read it as 8.8.8.8
    ("0x7f000001", "http://0x7f000001"),
]


def bound_to(binding: Binding, vault) -> None:
    vault.put(REF, "real-key", purpose="p", bindings=[binding])


def binding_for(host: str, port: int | None = None) -> Binding:
    return Binding(host=host, port=port, location=HeaderLocation(kind="header", name="x-api-key"))


def test_register_refuses_host_outside_bindings(registry, vault):
    vault.put("provider:p1:api_key", "real-key", purpose="p", bindings=[B])
    with pytest.raises(BindingMismatchError):
        registry.register(
            ProviderRoute(
                id="p1", base_url="https://evil.example.net/v1", secret_ref="provider:p1:api_key"
            ),
            vault,
        )


def test_a_refused_route_is_not_stored(registry, vault):
    bound_to(B, vault)
    with pytest.raises(BindingMismatchError):
        registry.register(
            ProviderRoute(id="p1", base_url="https://evil.example.net/v1", secret_ref=REF), vault
        )
    assert registry.get("p1") is None


def test_register_then_get_roundtrip(registry, vault):
    bound_to(B, vault)
    route = ProviderRoute(id="p1", base_url="https://api.example.com/v1", secret_ref=REF)
    registry.register(route, vault)
    assert registry.get("p1") == route


def test_get_unknown_provider_is_none(registry):
    assert registry.get("nope") is None


def test_registering_an_existing_id_replaces_the_route(registry, vault):
    bound_to(B, vault)
    registry.register(
        ProviderRoute(id="p1", base_url="https://api.example.com/v1", secret_ref=REF), vault
    )
    replacement = ProviderRoute(id="p1", base_url="https://api.example.com/v2", secret_ref=REF)
    registry.register(replacement, vault)
    assert registry.get("p1") == replacement


def test_routes_survive_reopening_the_registry(registry, registry_db_path, vault):
    bound_to(B, vault)
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    registry.register(route, vault)
    assert ProviderRegistry(registry_db_path).get("p1") == route


def test_a_keyless_route_needs_no_secret(registry, vault):
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1", secret_ref=None)
    registry.register(route, vault)
    assert registry.get("local") == route


def test_register_with_an_unknown_secret_raises_key_error(registry, vault):
    with pytest.raises(KeyError):
        registry.register(
            ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref="nope"), vault
        )
    assert registry.get("p1") is None


def test_host_comparison_ignores_case(registry, vault):
    bound_to(B, vault)
    route = ProviderRoute(id="p1", base_url="https://API.Example.COM/v1", secret_ref=REF)
    registry.register(route, vault)
    assert registry.get("p1") == route


def test_a_subdomain_or_a_parent_domain_is_not_the_bound_host(registry, vault):
    bound_to(B, vault)
    for base_url in ("https://eu.api.example.com", "https://example.com", "https://api.example.co"):
        with pytest.raises(BindingMismatchError):
            registry.register(ProviderRoute(id="p1", base_url=base_url, secret_ref=REF), vault)


def test_a_binding_without_port_allows_any_port(registry, vault):
    bound_to(B, vault)
    registry.register(
        ProviderRoute(id="p1", base_url="https://api.example.com:8443", secret_ref=REF), vault
    )
    assert registry.get("p1") is not None


def test_a_binding_with_a_port_requires_that_port(registry, vault):
    pinned = Binding(
        host="api.example.com",
        port=8443,
        location=HeaderLocation(kind="header", name="x-api-key"),
    )
    bound_to(pinned, vault)
    registry.register(
        ProviderRoute(id="p1", base_url="https://api.example.com:8443/v1", secret_ref=REF), vault
    )
    for base_url in ("https://api.example.com", "https://api.example.com:9443"):
        with pytest.raises(BindingMismatchError):
            registry.register(ProviderRoute(id="p2", base_url=base_url, secret_ref=REF), vault)


@pytest.mark.parametrize(
    ("host", "bound_port", "base_url", "accepted"),
    [
        ("api.example.com", 443, "https://api.example.com", True),
        ("api.example.com", 443, "https://api.example.com:443", True),
        ("fake-llm", 80, "http://fake-llm", True),
        ("fake-llm", 443, "http://fake-llm", False),
        ("api.example.com", 80, "https://api.example.com", False),
    ],
)
def test_the_default_port_of_the_scheme_counts_as_the_port(
    registry, vault, host, bound_port, base_url, accepted
):
    bound_to(binding_for(host, bound_port), vault)
    route = ProviderRoute(id="p1", base_url=base_url, secret_ref=REF)
    if accepted:
        registry.register(route, vault)
        assert registry.get("p1") == route
    else:
        with pytest.raises(BindingMismatchError):
            registry.register(route, vault)


def test_any_matching_binding_is_enough(registry, vault):
    other = Binding(
        host="evil.example.net", location=HeaderLocation(kind="header", name="x-api-key")
    )
    vault.put(REF, "real-key", purpose="p", bindings=[other, B])
    registry.register(
        ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF), vault
    )
    assert registry.get("p1") is not None


def test_the_registry_creates_missing_parent_directories(tmp_path):
    ProviderRegistry(tmp_path / "data" / "nested" / "providers.db")
    assert (tmp_path / "data" / "nested" / "providers.db").exists()


# --- Plain http: only to a local host -----------------------------------------------------------


@pytest.mark.parametrize(("host", "base_url"), LOCAL_HTTP_ROUTES)
def test_a_keyed_route_may_use_plain_http_to_a_local_host(registry, vault, host, base_url):
    bound_to(binding_for(host), vault)
    route = ProviderRoute(id="p1", base_url=base_url, secret_ref=REF)
    registry.register(route, vault)
    assert registry.get("p1") == route


@pytest.mark.parametrize(("host", "base_url"), PUBLIC_HTTP_ROUTES)
def test_a_keyed_route_must_use_https_to_any_other_host(registry, vault, host, base_url):
    bound_to(binding_for(host), vault)
    with pytest.raises(InsecureTransportError) as excinfo:
        registry.register(ProviderRoute(id="p1", base_url=base_url, secret_ref=REF), vault)
    assert isinstance(excinfo.value, BindingMismatchError)
    assert "real-key" not in str(excinfo.value)
    assert registry.get("p1") is None


def test_a_keyless_route_may_use_plain_http_to_any_host(registry, vault):
    route = ProviderRoute(id="p1", base_url="http://api.example.com/v1", secret_ref=None)
    registry.register(route, vault)
    assert registry.get("p1") == route


def test_https_is_fine_to_a_local_host(registry, vault):
    bound_to(binding_for("192.168.1.10"), vault)
    route = ProviderRoute(id="p1", base_url="https://192.168.1.10:8443", secret_ref=REF)
    registry.register(route, vault)
    assert registry.get("p1") == route
