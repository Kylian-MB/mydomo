import asyncio
import gzip
import logging
import sqlite3
import zlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import anyio
import httpx
import pytest
from domo_common.gatekeeper_api import Binding, InjectionLocation, ProviderRoute
from domo_gatekeeper.errors import (
    BindingMismatchError,
    InsecureTransportError,
    InvalidRelayPath,
    InvalidSecretValueError,
    UpstreamError,
    UpstreamTimeoutError,
    VaultDecryptError,
)
from domo_gatekeeper.relay import _DECODED_ENCODINGS, RelayResponse, relay
from domo_gatekeeper.vault import Vault
from httpx._decoders import SUPPORTED_DECODERS
from pydantic import TypeAdapter

B = Binding(host="api.example.com", location=InjectionLocation(kind="header", name="x-api-key"))
B_AUTH = Binding(
    host="api.example.com",
    location=InjectionLocation(kind="header", name="authorization", template="Bearer {value}"),
)
OTHER_HOST = Binding(
    host="evil.example.net", location=InjectionLocation(kind="header", name="x-other")
)
REF = "provider:p1:api_key"
URL = "https://api.example.com/v1/messages"


async def call(
    route: ProviderRoute,
    vault: Vault,
    client: httpx.AsyncClient,
    *,
    method: str = "POST",
    path: str = "v1/messages",
    query: str = "",
    headers: Mapping[str, str] | None = None,
    body: bytes = b"{}",
) -> RelayResponse:
    return await relay(
        route,
        method=method,
        path=path,
        query=query,
        headers=headers or {},
        body=body,
        vault=vault,
        client=client,
    )


async def read(resp: RelayResponse) -> bytes:
    return b"".join([chunk async for chunk in resp.body])


def header_map(resp: RelayResponse) -> dict[str, str]:
    return {name.lower(): value for name, value in resp.headers}


class TrackingStream(httpx.AsyncByteStream):
    """An upstream body that records whether it was closed."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        fail_with: Exception | None = None,
        block_forever: bool = False,
    ) -> None:
        self.chunks = chunks
        self.fail_with = fail_with
        self.block_forever = block_forever
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.fail_with is not None:
            raise self.fail_with
        if self.block_forever:
            await anyio.sleep_forever()

    async def aclose(self) -> None:
        # A checkpoint: closing from a cancelled scope only completes when it is shielded.
        await anyio.sleep(0)
        self.closed = True


def streaming_client(
    stream: TrackingStream,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def rewrite_bindings(db: Path, ref: str, bindings: list[Binding]) -> None:
    payload = TypeAdapter(list[Binding]).dump_json(bindings).decode()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE secrets SET bindings_json = ? WHERE ref = ?", (payload, ref))


# --- Key injection ------------------------------------------------------------------------------


async def test_injects_key_and_strips_caller_auth(respx_mock, vault, http_client):
    vault.put("provider:p1:api_key", "real-key", purpose="p", bindings=[B])
    upstream = respx_mock.post("https://api.example.com/v1/messages").respond(
        200, json={"ok": True}
    )
    route = ProviderRoute(
        id="p1", base_url="https://api.example.com", secret_ref="provider:p1:api_key"
    )
    resp = await relay(
        route,
        method="POST",
        path="v1/messages",
        query="",
        headers={
            "x-api-key": "core-token",
            "authorization": "Bearer core-token",
            "content-type": "application/json",
        },
        body=b"{}",
        vault=vault,
        client=http_client,
    )
    assert resp.status == 200
    sent = upstream.calls.last.request
    assert sent.headers["x-api-key"] == "real-key" and "authorization" not in sent.headers


async def test_the_binding_template_shapes_the_header_value(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[B_AUTH])
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    upstream = respx_mock.post(URL).respond(200)
    await call(route, vault, http_client, headers={"x-api-key": "core-token"})
    sent = upstream.calls.last.request
    assert sent.headers["authorization"] == "Bearer real-key"
    assert "x-api-key" not in sent.headers


async def test_every_binding_for_the_destination_is_injected(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[B, B_AUTH])
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    upstream = respx_mock.post(URL).respond(200)
    await call(route, vault, http_client)
    sent = upstream.calls.last.request
    assert sent.headers["x-api-key"] == "real-key"
    assert sent.headers["authorization"] == "Bearer real-key"


async def test_bindings_for_other_destinations_are_not_injected(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[B, OTHER_HOST])
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    upstream = respx_mock.post(URL).respond(200)
    await call(route, vault, http_client)
    sent = upstream.calls.last.request
    assert sent.headers["x-api-key"] == "real-key" and "x-other" not in sent.headers


async def test_the_caller_cannot_pick_the_value_of_the_injected_header(
    respx_mock, vault, http_client, route_p1
):
    upstream = respx_mock.post(URL).respond(200)
    await call(route_p1, vault, http_client, headers={"X-Api-Key": "attacker-chosen"})
    sent = upstream.calls.last.request
    assert sent.headers.get_list("x-api-key") == ["real-key"]


async def test_credentials_and_connection_headers_are_not_forwarded(
    respx_mock, vault, http_client, route_p1
):
    upstream = respx_mock.post(URL).respond(200)
    await call(
        route_p1,
        vault,
        http_client,
        headers={
            "Authorization": "Bearer core-token",
            "X-Goog-Api-Key": "core-token",
            "Cookie": "session=abc",
            "Proxy-Authorization": "Basic Y29yZQ==",
            "Host": "evil.example.net",
            "Content-Length": "999",
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "TE": "trailers",
            "Trailer": "x-trailer",
            "Transfer-Encoding": "chunked",
            "Upgrade": "websocket",
            "Proxy-Authenticate": "Basic",
            "Content-Type": "application/json",
            "Anthropic-Version": "2023-06-01",
            "X-Custom": "kept",
        },
        body=b'{"a":1}',
    )
    sent = upstream.calls.last.request
    for dropped in (
        "authorization",
        "x-goog-api-key",
        "cookie",
        "proxy-authorization",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
    ):
        assert dropped not in sent.headers, dropped
    assert sent.headers["host"] == "api.example.com"
    assert sent.headers["content-length"] == "7"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["x-custom"] == "kept"
    assert sent.headers["x-api-key"] == "real-key"


async def test_headers_named_by_the_connection_header_are_not_forwarded(
    respx_mock, vault, http_client, route_p1
):
    upstream = respx_mock.post(URL).respond(200)
    await call(
        route_p1,
        vault,
        http_client,
        headers={"Connection": "close, X-Hop", "X-Hop": "1", "X-Kept": "1"},
    )
    sent = upstream.calls.last.request
    assert "x-hop" not in sent.headers and sent.headers["x-kept"] == "1"


async def test_method_query_and_body_are_forwarded(respx_mock, vault, http_client, route_p1):
    upstream = respx_mock.put("https://api.example.com/v1/things").respond(200)
    await call(
        route_p1,
        vault,
        http_client,
        method="put",
        path="v1/things",
        query="a=1&b=x%20y",
        body=b"abc",
    )
    sent = upstream.calls.last.request
    assert sent.method == "PUT"
    assert str(sent.url) == "https://api.example.com/v1/things?a=1&b=x%20y"
    assert sent.content == b"abc"


async def test_a_get_without_a_body_sends_none(respx_mock, vault, http_client, route_p1):
    upstream = respx_mock.get("https://api.example.com/v1/models").respond(200)
    await call(route_p1, vault, http_client, method="GET", path="v1/models", body=b"")
    assert upstream.calls.last.request.content == b""


async def test_the_key_use_is_recorded(respx_mock, vault, http_client, route_p1):
    respx_mock.post(URL).respond(200)
    assert vault.metadata(REF).last_used_at is None
    await call(route_p1, vault, http_client)
    assert vault.metadata(REF).last_used_at is not None


async def test_keyless_route_forwards_without_injection(respx_mock, vault, http_client):
    up = respx_mock.post("http://192.168.1.10:11434/v1/chat/completions").respond(200, json={})
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1", secret_ref=None)
    await relay(
        route,
        method="POST",
        path="chat/completions",
        query="",
        headers={"authorization": "Bearer core-token"},
        body=b"{}",
        vault=vault,
        client=http_client,
    )
    assert "authorization" not in up.calls.last.request.headers


async def test_keyless_route_still_strips_every_credential_header(respx_mock, vault, http_client):
    up = respx_mock.post("http://192.168.1.10:11434/v1/chat/completions").respond(200)
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1", secret_ref=None)
    await call(
        route,
        vault,
        http_client,
        path="chat/completions",
        headers={"x-api-key": "core-token", "x-goog-api-key": "core-token", "cookie": "a=b"},
    )
    sent = up.calls.last.request
    assert not {"x-api-key", "x-goog-api-key", "cookie"} & set(sent.headers)


async def test_a_keyless_route_does_not_touch_the_vault_key_usage(
    respx_mock, vault, http_client, route_p1
):
    respx_mock.post("http://192.168.1.10:11434/v1/chat/completions").respond(200)
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1", secret_ref=None)
    await call(route, vault, http_client, path="chat/completions")
    assert vault.metadata(REF).last_used_at is None


async def test_the_url_is_the_base_url_plus_the_path(respx_mock, vault, http_client):
    up = respx_mock.get("http://192.168.1.10:11434/v1/models").respond(200)
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1/", secret_ref=None)
    await call(route, vault, http_client, method="GET", path="models")
    assert str(up.calls.last.request.url) == "http://192.168.1.10:11434/v1/models"


@pytest.mark.parametrize(
    "path",
    [
        "v1/messages",
        "v1beta/models/gemini-2.0-flash:streamGenerateContent",
        "v1/models/a%3Ab",
        "a.b/c_d/e-f",
        "",
    ],
)
async def test_acceptable_paths_are_forwarded(respx_mock, vault, http_client, route_p1, path):
    respx_mock.route().respond(200)
    await call(route_p1, vault, http_client, path=path)
    sent = respx_mock.calls.last.request
    assert str(sent.url) == f"https://api.example.com/{path}"


# --- Plain http: only to a local host -----------------------------------------------------------

LOCAL_HTTP_ROUTES = [
    ("192.168.1.10", "http://192.168.1.10:11434/v1"),
    ("127.0.0.1", "http://127.0.0.1"),
    ("localhost", "http://localhost:8080"),
    ("fake-llm", "http://fake-llm:9000/v1"),
    ("10.1.2.3", "http://10.1.2.3"),
    ("169.254.1.1", "http://169.254.1.1"),
]
PUBLIC_HTTP_ROUTES = [
    "http://api.example.com",
    "http://api.example.com:443",
    "http://api.example.com:8080/v1",
    "HTTP://API.Example.com",
]


def binding_for(host: str, port: int | None = None) -> Binding:
    return Binding(
        host=host, port=port, location=InjectionLocation(kind="header", name="x-api-key")
    )


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("base_url", PUBLIC_HTTP_ROUTES)
async def test_the_key_is_never_sent_in_cleartext_to_a_public_host(
    respx_mock, vault, http_client, base_url
):
    # Even for a route that never went through the registry, and a host the secret is bound to.
    vault.put(REF, "real-key", purpose="p", bindings=[B])
    anywhere = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url=base_url, secret_ref=REF)
    with pytest.raises(InsecureTransportError) as excinfo:
        await call(route, vault, http_client)
    assert isinstance(excinfo.value, BindingMismatchError)
    assert "real-key" not in f"{excinfo.value!s} {excinfo.value!r}"
    assert not anywhere.called
    assert vault.metadata(REF).last_used_at is None


@pytest.mark.parametrize(("host", "base_url"), LOCAL_HTTP_ROUTES)
async def test_the_key_may_cross_plain_http_to_a_local_host(
    respx_mock, vault, http_client, host, base_url
):
    vault.put(REF, "real-key", purpose="p", bindings=[binding_for(host)])
    upstream = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url=base_url, secret_ref=REF)
    await call(route, vault, http_client, path="chat/completions")
    sent = upstream.calls.last.request
    assert sent.headers["x-api-key"] == "real-key"
    assert sent.url.scheme == "http" and sent.url.host == host


async def test_a_keyless_route_may_use_plain_http_to_any_host(respx_mock, vault, http_client):
    upstream = respx_mock.get("http://api.example.com/v1/models").respond(200)
    route = ProviderRoute(id="p1", base_url="http://api.example.com/v1", secret_ref=None)
    await call(route, vault, http_client, method="GET", path="models")
    assert upstream.called


# --- Which destination may receive the key ------------------------------------------------------


@pytest.mark.respx(assert_all_called=False)
async def test_the_key_is_never_sent_to_a_host_outside_the_bindings(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[B])
    evil = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url="https://evil.example.net/v1", secret_ref=REF)
    with pytest.raises(BindingMismatchError) as excinfo:
        await call(route, vault, http_client)
    assert not evil.called
    assert "real-key" not in str(excinfo.value) and "real-key" not in repr(excinfo.value)
    assert vault.metadata(REF).last_used_at is None


PINNED_TO_443 = Binding(
    host="api.example.com", port=443, location=InjectionLocation(kind="header", name="x-api-key")
)


@pytest.mark.respx(assert_all_called=False)
async def test_the_key_is_never_sent_to_a_port_outside_the_bindings(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[PINNED_TO_443])
    anywhere = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url="https://api.example.com:8443", secret_ref=REF)
    with pytest.raises(BindingMismatchError):
        await call(route, vault, http_client)
    assert not anywhere.called


@pytest.mark.respx(assert_all_called=False)
async def test_a_local_host_pinned_to_a_port_refuses_another_port(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[binding_for("fake-llm", 9000)])
    anywhere = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url="http://fake-llm:9001/v1", secret_ref=REF)
    with pytest.raises(BindingMismatchError) as excinfo:
        await call(route, vault, http_client)
    assert not isinstance(excinfo.value, InsecureTransportError)
    assert not anywhere.called


async def test_a_pinned_port_matches_the_default_port_of_the_scheme(respx_mock, vault, http_client):
    vault.put(REF, "real-key", purpose="p", bindings=[PINNED_TO_443])
    upstream = respx_mock.post(URL).respond(200)
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    await call(route, vault, http_client)
    assert upstream.calls.last.request.headers["x-api-key"] == "real-key"


@pytest.mark.respx(assert_all_called=False)
async def test_edited_bindings_in_the_database_stop_the_call(
    respx_mock, vault, vault_db_path, http_client
):
    vault.put(REF, "real-key", purpose="p", bindings=[B])
    evil = respx_mock.route().respond(200)
    rewrite_bindings(vault_db_path, REF, [OTHER_HOST])
    route = ProviderRoute(id="p1", base_url="https://evil.example.net", secret_ref=REF)
    with pytest.raises(VaultDecryptError):
        await call(route, vault, http_client)
    assert not evil.called


@pytest.mark.respx(assert_all_called=False)
async def test_a_call_to_an_unknown_secret_fails_before_any_request(respx_mock, vault, http_client):
    anywhere = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref="provider:x:key")
    with pytest.raises(KeyError):
        await call(route, vault, http_client)
    assert not anywhere.called


async def test_the_destination_is_checked_with_one_authenticated_read(
    respx_mock, vault, http_client, route_p1, monkeypatch
):
    respx_mock.post(URL).respond(200)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the relay must not check and decrypt in two separate reads")

    reads: list[str] = []
    real_reveal_bound = Vault.reveal_bound

    def spy(self: Vault, ref: str):
        reads.append(ref)
        return real_reveal_bound(self, ref)

    monkeypatch.setattr(Vault, "metadata", forbidden)
    monkeypatch.setattr(Vault, "reveal", forbidden)
    monkeypatch.setattr(Vault, "reveal_bound", spy)
    await call(route_p1, vault, http_client)
    assert reads == [REF]


# --- Paths --------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../admin", "/abs", "https://evil.example.net/x", "a//b"])
async def test_rejects_bad_paths(bad, vault, http_client, route_p1):
    with pytest.raises(InvalidRelayPath):
        await relay(
            route_p1,
            method="GET",
            path=bad,
            query="",
            headers={},
            body=b"",
            vault=vault,
            client=http_client,
        )


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "a/..",
        "a/../b",
        "a/b/../../c",
        "a..b",
        "//evil.example.net/x",
        "a\\b",
        "..\\x",
        "%2e%2e/x",
        "a/%2E%2e/b",
        "a%2fb",
        "a%2Fb",
        "a%5cb",
        "a%5Cb",
        "mailto:someone",
        "javascript:alert(1)",
        "c:x",
        "http:x",
        "a b",
        "a\tb",
        "a\nb",
        "a\r\nHost: evil.example.net",
        "a\x00b",
        "a\x7fb",
        "a?x=1",
        "a#frag",
        "a\xa0b",
    ],
)
async def test_rejects_more_dangerous_paths(bad, vault, http_client, route_p1):
    with pytest.raises(InvalidRelayPath):
        await call(route_p1, vault, http_client, path=bad)


@pytest.mark.parametrize("bad", ["a=1\r\nHost: evil.example.net", "a=1#frag", "a=1 b", "a=\x00"])
async def test_rejects_queries_that_could_change_the_request(bad, vault, http_client, route_p1):
    with pytest.raises(InvalidRelayPath):
        await call(route_p1, vault, http_client, query=bad)


async def test_a_bad_path_is_refused_before_the_vault_is_read(vault, http_client):
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref="provider:x:key")
    with pytest.raises(InvalidRelayPath):
        await call(route, vault, http_client, path="../admin")


# --- Responses ----------------------------------------------------------------------------------


async def test_response_echoing_key_is_scrubbed(respx_mock, vault, http_client, route_p1):
    respx_mock.post("https://api.example.com/v1/messages").respond(200, text="your key is real-key")
    resp = await relay(
        route_p1,
        method="POST",
        path="v1/messages",
        query="",
        headers={},
        body=b"{}",
        vault=vault,
        client=http_client,
    )
    body = b"".join([c async for c in resp.body])
    assert b"real-key" not in body and b"[REDACTED]" in body


async def test_status_body_and_headers_are_relayed(respx_mock, vault, http_client, route_p1):
    respx_mock.post(URL).respond(
        201,
        content=b'{"id":"m1"}',
        headers=[("content-type", "application/json"), ("x-request-id", "r-1")],
    )
    resp = await call(route_p1, vault, http_client)
    assert resp.status == 201
    assert header_map(resp)["content-type"] == "application/json"
    assert header_map(resp)["x-request-id"] == "r-1"
    assert await read(resp) == b'{"id":"m1"}'


async def test_error_statuses_are_relayed_as_they_are(respx_mock, vault, http_client, route_p1):
    respx_mock.post(URL).respond(429, json={"error": "slow down"}, headers={"retry-after": "3"})
    resp = await call(route_p1, vault, http_client)
    assert resp.status == 429 and header_map(resp)["retry-after"] == "3"
    assert b"slow down" in await read(resp)


async def test_repeated_response_headers_are_kept(respx_mock, vault, http_client, route_p1):
    respx_mock.post(URL).respond(200, headers=[("x-multi", "1"), ("x-multi", "2")])
    resp = await call(route_p1, vault, http_client)
    assert [value for name, value in resp.headers if name.lower() == "x-multi"] == ["1", "2"]


async def test_hop_by_hop_and_length_headers_are_not_relayed(
    respx_mock, vault, http_client, route_p1
):
    respx_mock.post(URL).respond(
        200,
        content=b"hello",
        headers={
            "Connection": "close, x-hop",
            "X-Hop": "1",
            "Keep-Alive": "timeout=5",
            "Transfer-Encoding": "chunked",
            "Upgrade": "h2c",
            "Proxy-Authenticate": "Basic",
            "Trailer": "x-t",
            "TE": "trailers",
            "Content-Length": "5",
            "X-Kept": "1",
        },
    )
    resp = await call(route_p1, vault, http_client)
    names = {name.lower() for name, _ in resp.headers}
    assert not names & {
        "connection",
        "x-hop",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "trailer",
        "te",
        "content-length",
        "content-encoding",
    }
    assert "x-kept" in names


async def test_secret_values_in_response_headers_are_scrubbed(
    respx_mock, vault, http_client, route_p1
):
    respx_mock.post(URL).respond(200, headers={"x-debug": "auth was real-key ok"})
    resp = await call(route_p1, vault, http_client)
    assert header_map(resp)["x-debug"] == "auth was [REDACTED] ok"


async def test_every_secret_of_the_vault_is_scrubbed_not_only_the_injected_one(
    respx_mock, vault, http_client, route_p1
):
    vault.put("provider:p2:api_key", "another-provider-key", purpose="p", bindings=[OTHER_HOST])
    respx_mock.post(URL).respond(200, text="real-key and another-provider-key")
    assert await read(await call(route_p1, vault, http_client)) == b"[REDACTED] and [REDACTED]"


async def test_a_keyless_route_scrubs_the_secrets_of_the_vault_too(
    respx_mock, vault, http_client, route_p1
):
    respx_mock.get("http://192.168.1.10:11434/v1/models").respond(200, text="the key is real-key")
    route = ProviderRoute(id="local", base_url="http://192.168.1.10:11434/v1", secret_ref=None)
    resp = await call(route, vault, http_client, method="GET", path="models")
    assert await read(resp) == b"the key is [REDACTED]"


async def test_a_secret_split_across_upstream_chunks_is_scrubbed(vault, route_p1):
    stream = TrackingStream([b"your key is real-", b"key, bye"])
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        assert await read(resp) == b"your key is [REDACTED], bye"


async def test_the_response_is_streamed_not_buffered(vault, route_p1):
    # The upstream never finishes: what it already sent must still come out.
    stream = TrackingStream([b"hello " * 20], block_forever=True)
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        first = await asyncio.wait_for(anext(resp.body), timeout=2)
        assert first.startswith(b"hello hello ")
        await resp.aclose()


async def test_redirects_are_not_followed_and_the_key_stays_home(respx_mock, vault, route_p1):
    respx_mock.post(URL).respond(302, headers={"location": "https://evil.example.net/steal"})
    evil = respx_mock.route(host="evil.example.net").respond(200)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await call(route_p1, vault, client)
        assert resp.status == 302
        await read(resp)
    assert not evil.called


# --- Compression --------------------------------------------------------------------------------


async def test_compression_is_not_requested_from_the_provider(
    respx_mock, vault, http_client, route_p1
):
    upstream = respx_mock.post(URL).respond(200)
    await call(route_p1, vault, http_client, headers={"accept-encoding": "gzip, br, zstd"})
    assert upstream.calls.last.request.headers["accept-encoding"] == "identity"


async def test_a_compressed_response_is_scrubbed_on_its_decoded_content(
    respx_mock, vault, http_client, route_p1
):
    compressed = gzip.compress(b"your key is real-key, really")
    respx_mock.post(URL).respond(
        200,
        content=compressed,
        headers={"content-encoding": "gzip", "content-length": str(len(compressed))},
    )
    resp = await call(route_p1, vault, http_client)
    body = await read(resp)
    assert body == b"your key is [REDACTED], really"
    names = {name.lower() for name, _ in resp.headers}
    assert "content-encoding" not in names and "content-length" not in names


async def test_a_compressed_key_split_across_decoded_chunks_is_scrubbed(vault, route_p1):
    compressed = gzip.compress(b"your key is real-key, really")
    stream = TrackingStream([compressed[:9], compressed[9:20], compressed[20:]])
    async with streaming_client(stream, headers={"content-encoding": "gzip"}) as client:
        resp = await call(route_p1, vault, client)
        assert await read(resp) == b"your key is [REDACTED], really"


async def test_an_encoding_that_cannot_be_decoded_is_refused_and_closed(vault, route_p1):
    stream = TrackingStream([b"\x1b\x00opaque"])
    async with streaming_client(stream, headers={"content-encoding": "br"}) as client:
        with pytest.raises(UpstreamError):
            await call(route_p1, vault, client)
        assert stream.closed


PAYLOAD = b"your key is real-key, really"


@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("identity", PAYLOAD),
        ("gzip", gzip.compress(PAYLOAD)),
        ("deflate", zlib.compress(PAYLOAD)),
        # Codings are listed in the order they were applied: gzip first, then deflate.
        ("gzip, deflate", zlib.compress(gzip.compress(PAYLOAD))),
        ("GZip,Deflate", zlib.compress(gzip.compress(PAYLOAD))),
    ],
)
async def test_identity_and_stacked_supported_encodings_are_accepted(vault, route_p1, coding, body):
    stream = TrackingStream([body[:7], body[7:]])
    async with streaming_client(stream, headers={"content-encoding": coding}) as client:
        resp = await call(route_p1, vault, client)
        assert await read(resp) == b"your key is [REDACTED], really"


async def test_a_coding_stacked_over_two_headers_is_accepted(vault, route_p1):
    stream = TrackingStream([zlib.compress(gzip.compress(PAYLOAD))])
    headers = [("content-encoding", "gzip"), ("content-encoding", "deflate")]
    async with streaming_client(stream, headers=headers) as client:
        resp = await call(route_p1, vault, client)
        assert await read(resp) == b"your key is [REDACTED], really"


@pytest.mark.parametrize(
    "coding",
    ["x-gzip", "X-Gzip", "gzip, x-gzip", "x-gzip, gzip", "gzip, br", "compress", "zstd", "unknown"],
)
async def test_a_coding_httpx_does_not_decode_is_refused_whole(vault, route_p1, coding):
    # Relayed as it is, the key would pass the scrubber still compressed.
    stream = TrackingStream([gzip.compress(PAYLOAD)])
    async with streaming_client(stream, headers={"content-encoding": coding}) as client:
        with pytest.raises(UpstreamError) as excinfo:
            await call(route_p1, vault, client)
        assert "real-key" not in str(excinfo.value)
        assert stream.closed


async def test_an_undecodable_coding_in_a_second_header_is_refused(vault, route_p1):
    stream = TrackingStream([gzip.compress(PAYLOAD)])
    headers = [("content-encoding", "gzip"), ("content-encoding", "x-gzip")]
    async with streaming_client(stream, headers=headers) as client:
        with pytest.raises(UpstreamError):
            await call(route_p1, vault, client)
        assert stream.closed


def test_the_decoded_codings_are_the_ones_httpx_always_decodes():
    # br and zstd are decoded only when their libraries are installed; they are refused anyway.
    always = set(SUPPORTED_DECODERS) - {"br", "zstd"}
    assert always == {"identity", "gzip", "deflate"}
    assert always == _DECODED_ENCODINGS


async def test_httpx_leaves_an_x_gzip_body_compressed():
    # The reason x-gzip is refused: httpx skips a coding it does not know.
    stream = TrackingStream([gzip.compress(PAYLOAD)])
    response = httpx.Response(200, headers={"content-encoding": "x-gzip"}, stream=stream)
    assert b"real-key" not in await response.aread()


# --- Closing the upstream response --------------------------------------------------------------


async def test_the_upstream_response_is_closed_when_the_body_is_fully_read(vault, route_p1):
    stream = TrackingStream([b"a", b"b"])
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        assert not stream.closed
        await read(resp)
        assert stream.closed


async def test_the_upstream_response_is_closed_when_the_body_fails(vault, route_p1):
    stream = TrackingStream([b"a"], fail_with=httpx.ReadError("connection reset"))
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        with pytest.raises(UpstreamError):
            await read(resp)
        assert stream.closed


async def test_the_upstream_response_is_closed_when_the_consumer_stops_early(vault, route_p1):
    stream = TrackingStream([b"a", b"b", b"c"])
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        async for _ in resp.body:
            break
        await resp.body.aclose()  # type: ignore[attr-defined]
        assert stream.closed


async def test_the_upstream_response_is_closed_when_the_consumer_is_cancelled(vault, route_p1):
    stream = TrackingStream([b"hello " * 20], block_forever=True)
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        first_chunk_read = asyncio.Event()

        async def consume() -> None:
            async for _ in resp.body:
                first_chunk_read.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(first_chunk_read.wait(), timeout=2)
        await asyncio.sleep(0.01)  # let it block on the next chunk
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


async def test_closing_completes_even_when_the_enclosing_scope_is_cancelled(vault, route_p1):
    stream = TrackingStream([b"a"], block_forever=True)
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        with anyio.move_on_after(0.05):
            async for _ in resp.body:
                pass
        assert stream.closed


async def test_the_response_can_be_closed_without_reading_its_body(vault, route_p1):
    stream = TrackingStream([b"a"])
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        await resp.aclose()
        assert stream.closed
        await resp.aclose()  # idempotent


async def test_a_refused_response_keeps_no_connection_open(vault, route_p1):
    stream = TrackingStream([b"x"])
    async with streaming_client(stream, headers={"content-encoding": "zstd"}) as client:
        with pytest.raises(UpstreamError):
            await call(route_p1, vault, client)
    assert stream.closed


# --- Upstream failures never leak the key -------------------------------------------------------


def assert_clean(error: BaseException) -> None:
    text = f"{error!s} {error!r}"
    assert "real-key" not in text
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("cannot connect, sent x-api-key: real-key"),
        httpx.RemoteProtocolError("server disconnected after real-key"),
        httpx.LocalProtocolError("Illegal header value b'real-key'"),
        httpx.UnsupportedProtocol("real-key"),
    ],
)
async def test_connection_errors_do_not_leak_the_key(
    respx_mock, vault, http_client, route_p1, failure
):
    respx_mock.post(URL).mock(side_effect=failure)
    with pytest.raises(UpstreamError) as excinfo:
        await call(route_p1, vault, http_client)
    assert not isinstance(excinfo.value, UpstreamTimeoutError)
    assert_clean(excinfo.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("timed out connecting with real-key"),
        httpx.ReadTimeout("timed out reading real-key"),
        httpx.PoolTimeout("real-key"),
    ],
)
async def test_timeouts_do_not_leak_the_key(respx_mock, vault, http_client, route_p1, failure):
    respx_mock.post(URL).mock(side_effect=failure)
    with pytest.raises(UpstreamTimeoutError) as excinfo:
        await call(route_p1, vault, http_client)
    assert_clean(excinfo.value)


async def test_a_failure_while_streaming_does_not_leak_the_key(vault, route_p1):
    stream = TrackingStream([b"a"], fail_with=httpx.ReadError("reset while sending real-key"))
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        with pytest.raises(UpstreamError) as excinfo:
            await read(resp)
        assert_clean(excinfo.value)


async def test_a_timeout_while_streaming_is_a_timeout_error(vault, route_p1):
    stream = TrackingStream([b"a"], fail_with=httpx.ReadTimeout("slow real-key"))
    async with streaming_client(stream) as client:
        resp = await call(route_p1, vault, client)
        with pytest.raises(UpstreamTimeoutError) as excinfo:
            await read(resp)
        assert_clean(excinfo.value)


async def test_the_key_never_reaches_the_logs(respx_mock, vault, http_client, route_p1, caplog):
    caplog.set_level(logging.DEBUG)
    respx_mock.post(URL).respond(200, text="real-key")
    await read(await call(route_p1, vault, http_client))
    respx_mock.post(URL).mock(side_effect=httpx.ConnectError("real-key"))
    with pytest.raises(UpstreamError):
        await call(route_p1, vault, http_client)
    assert "real-key" not in caplog.text


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("value", ["bad\nkey", "bad\rkey", "bad\x00key", "bad\x7fkey", "café-key"])
async def test_a_value_that_cannot_be_a_header_is_refused_without_leaking_it(
    respx_mock, vault, http_client, value
):
    vault.put(REF, value, purpose="p", bindings=[B])
    anywhere = respx_mock.route().respond(200)
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    with pytest.raises(InvalidSecretValueError) as excinfo:
        await call(route, vault, http_client)
    assert not anywhere.called
    text = f"{excinfo.value!s} {excinfo.value!r}"
    assert "bad" not in text and "caf" not in text
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None


async def test_a_value_with_spaces_and_a_tab_is_a_valid_header_value(
    respx_mock, vault, http_client
):
    vault.put(REF, "two words\tand-a-tab", purpose="p", bindings=[B])
    route = ProviderRoute(id="p1", base_url="https://api.example.com", secret_ref=REF)
    upstream = respx_mock.post(URL).respond(200)
    await call(route, vault, http_client)
    assert upstream.calls.last.request.headers["x-api-key"] == "two words\tand-a-tab"
