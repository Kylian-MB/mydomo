"""The security middleware: response headers and the Origin check. No database needed, but for
the words of a refusal (Domo's, once installed)."""

import httpx
import pytest
from domo_common.i18n import t
from domo_core.web.security import CONTENT_SECURITY_POLICY, SecurityMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from web_support import build_test_app

EVIL = "https://evil.example.net"
EXPECTED_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; frame-src 'self'; frame-ancestors 'none'; form-action 'self'"
    ),
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
}


@pytest.fixture(autouse=True)
def _ok_route(offline_app):
    """A route that needs no database and answers every method: the checks run before it."""

    async def ok(request: Request) -> Response:
        return PlainTextResponse("ok")

    offline_app.add_route("/ok", ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])


def assert_hardened(response: httpx.Response) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value, name


# --- headers ---------------------------------------------------------------------------------


def test_the_policy_is_exactly_the_documented_one():
    assert EXPECTED_HEADERS["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_security_headers_present(offline_client):
    r = await offline_client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert_hardened(r)


@pytest.mark.parametrize(
    "path", ["/login", "/static/app.css", "/static/htmx.min.js", "/no-such-page", "/"]
)
async def test_every_response_is_hardened(offline_client, path):
    r = await offline_client.get(path)
    assert_hardened(r)


async def test_refused_responses_are_hardened_too(offline_client):
    r = await offline_client.post("/ok", headers={"origin": EVIL})
    assert r.status_code == 403
    assert_hardened(r)


async def test_the_headers_cannot_be_weakened_by_a_route():
    async def lax_app(scope: Scope, receive: Receive, send: Send) -> None:
        response = Response(
            "x",
            headers={
                "Content-Security-Policy": "default-src *",
                "Referrer-Policy": "unsafe-url",
                "X-Content-Type-Options": "off",
            },
        )
        await response(scope, receive, send)

    transport = httpx.ASGITransport(app=SecurityMiddleware(lax_app))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/")
    assert_hardened(r)
    assert r.headers.get_list("content-security-policy") == [
        EXPECTED_HEADERS["content-security-policy"]
    ]


async def test_streamed_bodies_pass_through_intact():
    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def chunks():
            yield b"data: one\n\n"
            yield b"data: two\n\n"

        await StreamingResponse(chunks(), media_type="text/event-stream")(scope, receive, send)

    transport = httpx.ASGITransport(app=SecurityMiddleware(streaming_app))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/")
    assert r.text == "data: one\n\ndata: two\n\n"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert_hardened(r)


# --- caching ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/", "/no-such-page"])
async def test_dynamic_pages_are_never_cached(offline_client, path):
    r = await offline_client.get(path)
    assert r.headers["cache-control"] == "no-store"


async def test_a_refused_request_is_never_cached(offline_client):
    r = await offline_client.post("/ok", headers={"origin": EVIL})
    assert r.status_code == 403
    assert r.headers["cache-control"] == "no-store"


async def test_a_wrong_method_is_never_cached(offline_client):
    r = await offline_client.put("/login")
    assert r.status_code == 405
    assert r.headers["cache-control"] == "no-store"


async def test_static_files_are_left_to_be_cached(offline_client):
    for path in ("/static/app.css", "/static/htmx.min.js", "/static/icon.svg"):
        r = await offline_client.get(path)
        assert r.status_code == 200
        assert "no-store" not in r.headers.get("cache-control", ""), path


async def test_a_route_that_chose_its_own_cache_control_keeps_it(offline_app):
    async def cached(request: Request) -> Response:
        return PlainTextResponse("x", headers={"Cache-Control": "private, max-age=60"})

    offline_app.add_route("/cached", cached)
    transport = httpx.ASGITransport(app=offline_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/cached")
    assert r.headers.get_list("cache-control") == ["private, max-age=60"]


async def test_streamed_responses_are_never_cached_either():
    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def chunks():
            yield b"data: one\n\n"

        await StreamingResponse(chunks(), media_type="text/event-stream")(scope, receive, send)

    transport = httpx.ASGITransport(app=SecurityMiddleware(streaming_app))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/chat/stream")
    assert r.headers.get_list("cache-control") == ["no-store"]


async def test_only_the_static_folder_is_exempt():
    async def plain_app(scope: Scope, receive: Receive, send: Send) -> None:
        await Response("x")(scope, receive, send)

    transport = httpx.ASGITransport(app=SecurityMiddleware(plain_app))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        cached = {
            path: (await http.get(path)).headers.get("cache-control")
            for path in (
                "/static/x.css",
                "/static",
                "/staticx/x.css",
                "/static-files/x",
                "/a/static/x",
            )
        }
    assert cached == {
        "/static/x.css": None,
        "/static": None,
        "/staticx/x.css": "no-store",
        "/static-files/x": "no-store",
        "/a/static/x": "no-store",
    }


# --- Origin check ----------------------------------------------------------------------------


async def test_cross_origin_post_is_refused(offline_client):
    r = await offline_client.post("/ok", headers={"origin": EVIL})
    assert r.status_code == 403


async def test_a_refused_request_explains_itself_in_the_visitors_language(
    settings, session, clock, client, client_factory
):
    # During the installation, the fixed text of the visitor's language.
    installing = build_test_app(settings, session, clock)
    async with client_factory(installing) as visitor:
        english = await visitor.post("/ok", headers={"origin": EVIL})
        french = await visitor.post(
            "/ok", headers={"origin": EVIL, "accept-language": "fr-FR,fr;q=0.9"}
        )
    assert english.status_code == french.status_code == 403
    assert english.headers["content-type"].startswith("text/plain")
    assert english.text == t("security.origin_refused", "en")
    assert french.text == t("security.origin_refused", "fr") != english.text
    # Once installed, Domo's words: the default phrases of the visitor's language, which has
    # them; else the English ones.
    refused = await client.post(
        "/ok", headers={"origin": EVIL, "accept-language": "fr-FR,fr;q=0.9"}
    )
    assert refused.status_code == 403
    assert refused.text == t("security.origin_refused", "fr")
    german = await client.post("/ok", headers={"origin": EVIL, "accept-language": "de"})
    assert german.text == t("security.origin_refused", "en")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_every_unsafe_method_is_checked(offline_client, method):
    r = await offline_client.request(method, "/anything", headers={"origin": EVIL})
    assert r.status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("origin", [EVIL, "null", ""])
async def test_safe_methods_are_never_blocked(offline_client, method, origin):
    r = await offline_client.request(method, "/login", headers={"origin": origin})
    assert r.status_code != 403


async def test_safe_methods_without_any_origin_are_allowed(client_factory, offline_app):
    async with client_factory(offline_app, origin=None) as http:
        assert (await http.get("/login")).status_code == 200


async def test_the_same_origin_is_accepted(offline_client):
    r = await offline_client.post("/ok")
    assert r.status_code == 200


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("testserver", "https://testserver"),
        ("testserver", "https://TESTSERVER"),
        ("TestServer", "https://testserver"),
        ("testserver", "http://testserver"),  # the scheme is not compared, only host and port
        ("testserver:443", "https://testserver"),  # default port, written or not
        ("testserver", "https://testserver:443"),
        ("testserver:80", "http://testserver"),
        ("192.168.1.10:8443", "https://192.168.1.10:8443"),
        ("localhost:8443", "https://localhost:8443"),
        ("[::1]:8443", "https://[::1]:8443"),
        ("[::1]", "https://[::1]"),
    ],
)
async def test_same_host_and_port_is_accepted(offline_client, host, origin):
    r = await offline_client.post("/ok", headers={"host": host, "origin": origin})
    assert r.status_code == 200, (host, origin)


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("testserver", EVIL),
        ("testserver", "https://testserver.evil.example.net"),  # a suffix is not the host
        ("testserver", "https://eviltestserver"),
        ("testserver", "https://testserver:8443"),  # same host, other port
        ("testserver", "https://testserver:80"),
        ("192.168.1.10:8443", "https://192.168.1.10"),
        ("192.168.1.10:8443", "https://192.168.1.10:9443"),
        ("192.168.1.10:8443", "https://192.168.1.11:8443"),
        ("testserver", "null"),
        ("testserver", ""),
        ("testserver", "testserver"),  # not an origin
        ("testserver", "https://testserver/"),  # an origin has no path
        ("testserver", "https://testserver/login"),
        ("testserver", "https://user@testserver"),
        ("testserver", "https://testserver:99999"),
        ("testserver", "ftp://testserver"),
        ("testserver", "javascript://testserver"),
        ("testserver", "https://[::1"),
    ],
)
async def test_another_host_or_port_is_refused(offline_client, host, origin):
    r = await offline_client.post("/ok", headers={"host": host, "origin": origin})
    assert r.status_code == 403, (host, origin)


@pytest.mark.parametrize("host", ["", "evil.example.net@testserver", "testserver/x", "a b"])
async def test_a_malformed_host_is_refused(offline_client, host):
    r = await offline_client.post("/ok", headers={"host": host, "origin": "https://testserver"})
    assert r.status_code == 403


async def test_referer_is_used_when_there_is_no_origin(client_factory, offline_app):
    async with client_factory(offline_app, origin=None) as http:
        ok = await http.post("/ok", headers={"referer": "https://testserver/chat?x=1#top"})
        assert ok.status_code == 200
        for referer in (
            "https://evil.example.net/login",
            "https://testserver.evil.example.net/login",
            "https://testserver:8443/login",
            "https://user@testserver/login",
            "not a url",
            "",
        ):
            refused = await http.post("/ok", headers={"referer": referer})
            assert refused.status_code == 403, referer


async def test_origin_wins_over_referer(offline_client):
    r = await offline_client.post(
        "/ok", headers={"origin": EVIL, "referer": "https://testserver/login"}
    )
    assert r.status_code == 403


async def test_a_post_with_neither_origin_nor_referer_is_refused(client_factory, offline_app):
    async with client_factory(offline_app, origin=None) as http:
        assert (await http.post("/ok")).status_code == 403


async def test_a_refused_request_never_reaches_the_application():
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["method"])
        await PlainTextResponse("ok")(scope, receive, send)

    transport = httpx.ASGITransport(app=SecurityMiddleware(inner))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        assert (await http.post("/", headers={"origin": EVIL})).status_code == 403
        assert (await http.post("/", headers={"origin": "https://testserver"})).status_code == 200
    assert calls == ["POST"]


async def test_non_http_scopes_are_passed_through():
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = SecurityMiddleware(inner)

    async def receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, str]) -> None:
        raise AssertionError("nothing to send")

    await middleware({"type": "lifespan"}, receive, send)  # pyright: ignore[reportArgumentType]
    assert seen == ["lifespan"]
