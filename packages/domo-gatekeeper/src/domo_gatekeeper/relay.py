"""The relay: calls to a model provider, with the provider's key injected on the way out.

The core's SDKs send the core's internal token as their "API key". The relay drops it, adds the
real key from the vault at the place named by the secret's binding, and streams the answer back
with every secret value masked. The key never reaches the core: not in the request it sends, not
in the response, not in an error.

What guards the key:

* The destination is derived from the URL the request is actually sent to, and the key is
  injected only at the locations of the bindings that allow that host and port. The bindings
  come from the same authenticated read as the value (``Vault.reveal_bound``), so a writer
  cannot swap them between a check and the use.
* The path must stay below the provider's base URL, and the final URL must still point at the
  same scheme, host and port as the base URL.
* Redirects are never followed: the key would travel with them.
* A key goes over plain http only to a local host (``domo_common.transport.cleartext_allowed``):
  bindings name a host, not a scheme.
* Compression is not requested and, should the provider compress anyway, the body is scrubbed
  on its decoded bytes. httpx silently passes a body with a coding it does not know through
  undecoded (``x-gzip``, an uninstalled ``br``), so any coding outside ``_DECODED_ENCODINGS``
  is refused rather than relayed.
* Upstream failures are re-raised without the original error: HTTP libraries put header values
  in some of their messages.
"""

import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial

import anyio
import anyio.to_thread
import httpx
from domo_common.gatekeeper_api import HeaderLocation, ProviderRoute

from domo_gatekeeper.errors import (
    BindingMismatchError,
    InvalidRelayPath,
    InvalidSecretValueError,
    UpstreamError,
    UpstreamTimeoutError,
)
from domo_gatekeeper.providers import binding_allows, default_port, require_secure_transport
from domo_gatekeeper.scrub import Scrubber, scrub_text
from domo_gatekeeper.vault import Vault, secret_parts

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# What the caller must not decide: its own credentials, the target host and the body length.
_DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {
    "authorization",
    "x-api-key",
    "x-goog-api-key",
    "cookie",
    "host",
    "content-length",
}
# The body is rewritten by the scrubber, so its length and encoding no longer describe it.
_DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length", "content-encoding"}
# The content codings httpx always decodes (a test pins this to httpx's own table). Anything
# else would reach the scrubber still encoded. ``br`` and ``zstd`` are decoded only when their
# libraries are installed, so they are refused too.
_DECODED_ENCODINGS = frozenset({"identity", "gzip", "deflate"})

_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]*")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
# An encoded dot, slash or backslash: some servers decode them after the path was checked.
_ENCODED_SEPARATOR = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
# What the HTTP stack accepts as a header value: printable ASCII with inner blanks.
_HEADER_VALUE = re.compile(r"[\x21-\x7e](?:[\x20-\x7e\t]*[\x21-\x7e])?")


@dataclass
class RelayResponse:
    """A provider's answer: status and headers now, the scrubbed body as a stream.

    Reading ``body`` to its end (or failing, or being cancelled while reading it) releases the
    upstream connection. ``aclose`` does it explicitly, for a response whose body is never read.
    """

    status: int
    headers: list[tuple[str, str]]
    body: AsyncIterator[bytes]
    upstream: httpx.Response | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Release the upstream connection now. Safe to call more than once."""
        if self.upstream is not None:
            await _close(self.upstream)


async def _close(response: httpx.Response) -> None:
    # Shielded: closing must complete even when it runs because the caller was cancelled
    # (a client that disconnects mid-stream), or the connection would leak.
    with anyio.CancelScope(shield=True):
        await response.aclose()


def _check_path(path: str) -> None:
    if (
        _PRINTABLE_ASCII.fullmatch(path) is None
        or path.startswith("/")
        or "//" in path
        or ".." in path
        or "\\" in path
        or "?" in path
        or "#" in path
        or _SCHEME.match(path) is not None
        or _ENCODED_SEPARATOR.search(path) is not None
    ):
        raise InvalidRelayPath(
            "The relay path must be relative, free of '..', '//' and a scheme, "
            "and must not carry a query or a fragment."
        )


def _check_query(query: str) -> None:
    if _PRINTABLE_ASCII.fullmatch(query) is None or "#" in query:
        raise InvalidRelayPath("The relay query must be printable ASCII without a fragment.")


def _parse_url(url: str) -> httpx.URL | None:
    try:
        return httpx.URL(url)
    except httpx.InvalidURL:
        return None


def _target_url(route: ProviderRoute, path: str, query: str) -> httpx.URL:
    """The URL to call; it is guaranteed to stay on the provider's scheme, host and port."""
    _check_path(path)
    _check_query(query)
    base = _parse_url(route.base_url)
    url = _parse_url(f"{route.base_url.rstrip('/')}/{path}" + (f"?{query}" if query else ""))
    if (
        base is None
        or url is None
        or url.scheme != base.scheme
        or url.host != base.host
        or url.port != base.port
        or not url.path.startswith(base.path.rstrip("/") + "/")
    ):
        raise InvalidRelayPath("The relay path does not lead to a URL below the provider.")
    return url


def connection_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    """The header names a Connection header declares hop-by-hop."""
    return {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }


def _outgoing_headers(headers: Mapping[str, str]) -> dict[str, str]:
    dropped = _DROPPED_REQUEST_HEADERS | connection_tokens(headers.items())
    outgoing = {
        name.lower(): value for name, value in headers.items() if name.lower() not in dropped
    }
    # Compression is never requested: the answer is scrubbed on what it says, not on how it is
    # packed.
    outgoing["accept-encoding"] = "identity"
    return outgoing


def header_value(ref: str, template: str, value: str) -> str:
    # str.replace, not str.format: a template is data from the core, not a format string.
    header = template.replace("{value}", value)
    if _HEADER_VALUE.fullmatch(header) is None:
        # Said without the value, and raised outside any handler so nothing is chained to it.
        raise InvalidSecretValueError(f"The secret {ref!r} cannot be sent in an HTTP header.")
    return header


def _prepare(
    vault: Vault, ref: str | None, host: str, port: int
) -> tuple[dict[str, str], list[str]]:
    """The headers to inject and the values to scrub, from the vault (blocking calls)."""
    if ref is None:
        return {}, vault.values()
    value, bindings = vault.reveal_bound(ref)
    # A provider's key goes in a header only: an account (Basic, a login body) is never placed
    # here.
    allowed = sorted(
        (
            binding.location
            for binding in bindings
            if isinstance(binding.location, HeaderLocation) and binding_allows(binding, host, port)
        ),
        key=lambda location: (location.name, location.template),
    )
    if not allowed:
        raise BindingMismatchError(f"No binding of the secret {ref!r} permits {host}:{port}.")
    injected = {location.name: header_value(ref, location.template, value) for location in allowed}
    # The value is added to the list in case it was rotated since the vault was read.
    secrets = [*vault.values(), *secret_parts(value)]
    vault.mark_used(ref)
    return injected, secrets


def _upstream_error(exc: Exception) -> UpstreamError:
    """A failure to talk to the provider, described without anything from the original error."""
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamTimeoutError("The provider did not answer in time.")
    return UpstreamError(f"The request to the provider failed ({type(exc).__name__}).")


async def _send(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
    try:
        # Never follow redirects, whatever the client is configured to do: the injected key
        # would be sent to wherever the provider points.
        return await client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        error = _upstream_error(exc)
    raise error


def _is_decoded(response: httpx.Response) -> bool:
    encodings = (
        token.strip().lower()
        for value in response.headers.get_list("content-encoding")
        for token in value.split(",")
    )
    return all(encoding in _DECODED_ENCODINGS for encoding in encodings if encoding)


def _response_headers(response: httpx.Response, secrets: list[str]) -> list[tuple[str, str]]:
    items = response.headers.multi_items()
    dropped = _DROPPED_RESPONSE_HEADERS | connection_tokens(items)
    return [
        (name, scrub_text(value, secrets)) for name, value in items if name.lower() not in dropped
    ]


async def _scrubbed_body(response: httpx.Response, secrets: list[str]) -> AsyncIterator[bytes]:
    scrubber = Scrubber(secrets)
    error: UpstreamError | None = None
    try:
        try:
            # Decoded bytes: a secret inside a compressed body is not visible before decoding.
            async for chunk in response.aiter_bytes():
                if masked := scrubber.feed(chunk):
                    yield masked
            if tail := scrubber.flush():
                yield tail
        except httpx.HTTPError as exc:
            error = _upstream_error(exc)
    finally:
        await _close(response)
    if error is not None:
        raise error


async def relay(
    route: ProviderRoute,
    *,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    body: bytes,
    vault: Vault,
    client: httpx.AsyncClient,
) -> RelayResponse:
    """Call the provider of ``route`` and return its answer, scrubbed of every secret.

    ``path`` is relative to the provider's base URL. Raises ``InvalidRelayPath`` for a path or
    query that is not acceptable; ``BindingMismatchError`` when no binding of the route's secret
    allows the destination, or ``InsecureTransportError`` (a subclass) when a keyed route uses
    plain http to a host that is not local; ``UpstreamError`` (``UpstreamTimeoutError`` for a
    timeout) when the provider cannot be reached; and ``KeyError`` when the route's secret is not
    in the vault.
    """
    url = _target_url(route, path, query)
    if route.secret_ref is not None:
        # Judged on the URL httpx will request, not on what the route says.
        require_secure_transport(route.secret_ref, url.scheme, url.host)
    outgoing = _outgoing_headers(headers)
    # The vault does blocking I/O (SQLite, AES): keep it off the event loop.
    injected, secrets = await anyio.to_thread.run_sync(
        partial(_prepare, vault, route.secret_ref, url.host, url.port or default_port(url.scheme))
    )
    outgoing.update(injected)
    request = client.build_request(method, url, headers=outgoing, content=body or None)
    response = await _send(client, request)
    try:
        if not _is_decoded(response):
            raise UpstreamError("The provider used a content encoding that cannot be scrubbed.")
        response_headers = _response_headers(response, secrets)
    except BaseException:
        await _close(response)  # no connection is left open when the answer is refused
        raise
    return RelayResponse(
        status=response.status_code,
        headers=response_headers,
        body=_scrubbed_body(response, secrets),
        upstream=response,
    )
