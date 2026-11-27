"""Security middleware: response headers, and the Origin check on requests that change things.

It is written as a plain ASGI middleware (not ``BaseHTTPMiddleware``) so that streamed responses
(the chat's server-sent events) pass through untouched, and a client that disconnects is noticed
by the endpoint.

Every dynamic response (anything outside ``/static/``) carries ``Cache-Control: no-store``
unless its route chose a value of its own: pages are personal (the audit log, the chat) and must
not be kept by the browser or a proxy. Static files are left to be cached.

The Origin check is the protection against cross-site request forgery, on top of the session
cookie being ``SameSite=Strict``. The edge proxy preserves the ``Host`` header the browser sent,
so a request that really comes from the page names the same host and port in ``Origin`` (or, when
a browser leaves that out, in ``Referer``). The scheme is not compared: the core only ever sees
the plain HTTP the edge speaks to it. The refusal is said in the words its reader reads
(``web.texts_middleware.texts_for``).

The channel of the API (``channels.api``, under ``API_PATH``) is no page: a program speaks to it
with a device's secret (``Authorization: Bearer``), which no other site can make a browser send,
so its requests are not asked for an ``Origin``. A request there that carries the session cookie
of the page is refused (403, ``{"error": "browser_session"}``), whatever its method: the channel
knows no browser, and a page never reaches it with the person's cookie.

This middleware looks at ``http`` scopes only: a WebSocket (``websocket`` scope) passes through
it untouched. The one socket of the core, the page's voice (``/chat/voice``), makes its own check
before it accepts anything (``channels.voice.routes``): exactly one ``Origin`` header, compared
with ``is_same_origin`` (no ``Referer`` stands in for it), then the session cookie.
"""

import re
from typing import NamedTuple

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.channels.api import is_api_path
from domo_core.web.texts_middleware import texts_for

# ``script-src 'self' 'wasm-unsafe-eval'``: the page's speech detector is WebAssembly served from
# our own address (``static/voice/``). ``'wasm-unsafe-eval'`` lets WebAssembly compile and nothing
# else: it does not allow ``eval`` or ``new Function`` (that is ``'unsafe-eval'``). The detector
# runs on one thread, in the page, with no worker of its own, and the capture worklet is a
# same-origin script (``AudioWorklet.addModule`` follows ``script-src``), so no ``worker-src`` and
# no ``blob:`` are needed; the socket is same-origin (``connect-src`` falls back to
# ``default-src``, where ``'self'`` covers ``wss:`` on the same host).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; frame-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Permissions-Policy": "microphone=(self)",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

STATIC_PATH = "/static"

_DEFAULT_PORTS = {"http": 80, "https": 443}
# A host name, an IPv4 address or a bracketed IPv6 address, and an optional port: what a browser
# puts in ``Host`` and ``Origin``. No user information, no path.
_AUTHORITY = r"(?P<host>[a-z0-9._-]+|\[[0-9a-f:.]+\])(?::(?P<port>[0-9]{1,5}))?"
_AUTHORITY_RE = re.compile(_AUTHORITY, re.IGNORECASE)
_ORIGIN_RE = re.compile(rf"(?P<scheme>https?)://{_AUTHORITY}", re.IGNORECASE)
_REFERER_RE = re.compile(
    rf"(?P<scheme>https?)://{_AUTHORITY}(?:[/?#].*)?", re.IGNORECASE | re.DOTALL
)


class _Authority(NamedTuple):
    host: str
    port: int | None


def _authority(match: re.Match[str]) -> _Authority | None:
    port = match.group("port")
    if port is not None and int(port) > 65535:
        return None
    return _Authority(match.group("host").lower(), None if port is None else int(port))


def is_same_origin(headers: Headers) -> bool:
    """Whether a request that changes something comes from our own page.

    ``Origin`` is checked when the request has one (exactly one, and a valid one: ``null`` and
    anything malformed are refused), otherwise ``Referer``. A request with neither is refused.
    """
    hosts = headers.getlist("host")
    if len(hosts) != 1 or (match := _AUTHORITY_RE.fullmatch(hosts[0])) is None:
        return False
    host = _authority(match)
    origins = headers.getlist("origin")
    if origins:
        source = _ORIGIN_RE.fullmatch(origins[0]) if len(origins) == 1 else None
    else:
        referers = headers.getlist("referer")
        source = _REFERER_RE.fullmatch(referers[0]) if len(referers) == 1 else None
    if host is None or source is None or (origin := _authority(source)) is None:
        return False
    # An absent port is the default port of the scheme the browser used.
    default_port = _DEFAULT_PORTS[source.group("scheme").lower()]
    host_port = default_port if host.port is None else host.port
    origin_port = default_port if origin.port is None else origin.port
    return (host.host, host_port) == (origin.host, origin_port)


def apply_security_headers(headers: MutableHeaders) -> None:
    """Set the security headers, replacing whatever the endpoint put there."""
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value


def apply_cache_control(headers: MutableHeaders, path: str) -> None:
    """Forbid caching of the response to ``path``, unless it is a static file or the route
    already set a ``Cache-Control`` of its own."""
    is_static = path == STATIC_PATH or path.startswith(f"{STATIC_PATH}/")
    if not is_static and "cache-control" not in headers:
        headers["Cache-Control"] = "no-store"


class SecurityMiddleware:
    """Adds the security headers to every response and refuses cross-site unsafe requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                apply_security_headers(response_headers)
                apply_cache_control(response_headers, scope["path"])
            await send(message)

        if is_api_path(scope["path"]):
            # A program with a device's secret: no Origin to check, and never a browser's session.
            if SESSION_COOKIE in HTTPConnection(scope).cookies:
                refused = JSONResponse({"error": "browser_session"}, status_code=403)
                await refused(scope, receive, send_hardened)
                return
            await self.app(scope, receive, send_hardened)
            return
        headers = Headers(scope=scope)
        if scope["method"] in UNSAFE_METHODS and not is_same_origin(headers):
            # Refused before the texts of the request are read: they are read for the refusal.
            texts = await texts_for(scope)
            refusal = PlainTextResponse(texts("security.origin_refused"), status_code=403)
            await refusal(scope, receive, send_hardened)
            return
        await self.app(scope, receive, send_hardened)
