"""No secret reaches the logs, even with the root logger at DEBUG.

What is covered: a call relayed through the internal app, with the real ``httpx`` client from
``create_http_client`` talking over TCP to a local upstream, so that the ``httpx`` and ``httpcore``
logging paths actually run. The upstream echoes the key it receives in a response header and in
the body. The test asserts that the key does not appear in any record captured at DEBUG level
once the gatekeeper's logging setup (``pin_http_loggers``) is applied, and (as a control) that it
does appear when the setup is not applied, which shows that the test would catch the leak.

What is not covered: other libraries' loggers than those two, and a log sink other than pytest's
(uvicorn's own formatting of its error log).
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from domo_common.gatekeeper_api import Binding, HeaderLocation, ProviderRoute
from domo_gatekeeper.__main__ import create_http_client, pin_http_loggers
from domo_gatekeeper.internal_api import create_internal_app
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.vault import Vault
from fastapi import FastAPI

CORE_TOKEN = "core-token-for-log-tests"
KEY = "sk-log-test-0123456789abcdef"  # gitleaks:allow
HTTP_LOGGERS = ("httpcore", "httpx")


@pytest.fixture
def restore_http_loggers() -> Iterator[None]:
    levels = {name: logging.getLogger(name).level for name in HTTP_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
async def upstream_port() -> AsyncIterator[int]:
    """A local HTTP/1.1 server that echoes the ``x-api-key`` it receives, in header and body."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in head.split("\r\n")[1:])
        }
        await reader.readexactly(int(headers.get("content-length", "0")))
        key = headers.get("x-api-key", "")
        body = f'{{"echo": "{key}"}}'.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: application/json\r\n"
            + f"x-echo-key: {key}\r\n".encode()
            + f"content-length: {len(body)}\r\n".encode()
            + b"connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def relayed_app(
    upstream_port: int, vault: Vault, store: SecretRequestStore, registry: ProviderRegistry
) -> AsyncIterator[FastAPI]:
    """The internal app, wired to the production http client and a keyed local provider."""
    local = Binding(host="127.0.0.1", location=HeaderLocation(kind="header", name="x-api-key"))
    vault.put("provider:local:api_key", KEY, purpose="p", bindings=[local])
    registry.register(
        ProviderRoute(
            id="local",
            base_url=f"http://127.0.0.1:{upstream_port}",
            secret_ref="provider:local:api_key",
        ),
        vault,
    )
    async with create_http_client() as client:
        yield create_internal_app(
            vault=vault,
            requests=store,
            providers=registry,
            http_client=client,
            core_token=CORE_TOKEN,
        )


async def relayed_call(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        return await client.post(
            "/llm/local/v1/messages",
            headers={"authorization": f"Bearer {CORE_TOKEN}", "x-api-key": CORE_TOKEN},
            content=b'{"prompt": "hello"}',
        )


async def test_the_key_never_reaches_the_logs_at_debug_level(
    relayed_app, caplog, restore_http_loggers
):
    pin_http_loggers()
    caplog.set_level(logging.DEBUG)

    response = await relayed_call(relayed_app)

    # The call went through the real client, the key was injected and its echo masked...
    assert response.status_code == 200
    assert KEY not in response.text and KEY not in str(response.headers)
    assert response.headers["x-echo-key"] == "[REDACTED]"
    # ...and nothing sensitive was logged.
    assert KEY not in caplog.text
    assert CORE_TOKEN not in caplog.text


async def test_without_the_pin_httpcore_logs_the_echoed_key(
    relayed_app, caplog, restore_http_loggers
):
    # The control: httpcore at DEBUG logs the provider's response headers, unscrubbed.
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    await relayed_call(relayed_app)

    assert KEY in caplog.text


async def test_refusals_are_logged_without_credentials(
    relayed_app, registry_db_path, caplog, restore_http_loggers
):
    pin_http_loggers()
    caplog.set_level(logging.DEBUG)

    with sqlite3.connect(registry_db_path) as conn:
        conn.execute(
            "INSERT INTO providers (id, base_url, secret_ref) VALUES (?, ?, ?)",
            ("evil", "https://evil.example.net", "provider:local:api_key"),
        )

    transport = httpx.ASGITransport(app=relayed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gatekeeper.test") as client:
        response = await client.post(
            "/llm/evil/v1/messages",
            headers={"authorization": f"Bearer {CORE_TOKEN}", "x-custom": "custom-header-value"},
            content=b'{"prompt": "private prompt"}',
        )

    assert response.status_code == 403
    assert any(r.name == "domo_gatekeeper.internal_api" for r in caplog.records)
    for sensitive in (KEY, CORE_TOKEN, "custom-header-value", "private prompt"):
        assert sensitive not in caplog.text
