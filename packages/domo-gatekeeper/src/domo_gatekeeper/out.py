"""The relay for programs: a request a program of Domo sends, to an open address only.

The sandbox has no network: a program's request comes from the core (``POST /internal/out``),
with the addresses open to it, and the gatekeeper sends it. What guards it, in this order, for
the first request and for every redirect:

1. The URL is ``http`` or ``https`` and its host and port (the scheme's default when none is
   written) are one of the open addresses, compared on the name as written.
2. The name is resolved once, and the address it gives is judged (``reach.zone``, at that port):
   never reachable, the machine that hosts Domo (open only as such), or else. When the core
   says the zone it judged (``expect_zone``), the first address must be in it: else
   ``zone_changed``, and nothing is sent.
3. The connection goes to that address, the name kept for the ``Host`` header and for TLS (the
   server name and the certificate check, through httpcore's ``sni_hostname``): there is no
   second lookup, so a name that changes its target between the two opens nothing.
4. What identifies (``Authorization``, ``Cookie``, an API key, any header the account places)
   and the hop-by-hop headers are never the program's to send. Every request says it comes from
   a tool of Domo (``Via: 1.1 domo-out``): his own page refuses it, whatever leads to it.
5. An account may be used towards an address when at least one of its bindings allows that host
   and port, and it is placed by each binding that allows it; in clear only towards the house or
   an opened port of the host machine, and with the cookies of its session. A binding never
   places a header the relay decides (``NEVER_A_PLACE``: ``Via``, the framing, the cookies).
6. A redirect is followed five times at most, each hop through 1 to 4, never with the account
   nor its cookies, and the body a hop resends is the program's own. A hop never leaves the zone
   of the first address (the one the core judged): one to another zone is ``bad_redirect``, and
   a program that wants that address asks for it itself.
7. The answer is read whole up to ``most_bytes`` of decoded body, within ``seconds`` for the
   whole request; the cookies the answer to an account's request gets (not a hop's) go to its
   session and never back. What comes back is scrubbed of every secret part of the vault, and,
   for a request that carried an account, of its parts, of each value exactly as it was placed
   (the header as rendered, the Basic value and its base64 part, each login field as written in
   the body), so that their encoded forms are masked too, and of that account's session cookies
   of 8 characters or more (``Sessions.values``), never of another account's.
8. A refusal or a failure is its code (``OutRefused``), never the text of an error.

Compression is never asked for. Should the site compress anyway, one known coding (``gzip``,
``deflate``) is decoded here, bounded as it is decoded, so a small compressed body cannot blow
up in memory. Any other coding cannot be decoded, so cannot be scrubbed; several stacked are
refused too, though each is known, because a bound on the last decoding does not bound the ones
before it (a compressed bomb inside a compressed body). Both are ``unreachable``: the answer
cannot be read safely.
"""

import base64
import binascii
import json
import re
import ssl
import zlib
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from urllib.parse import parse_qsl, quote_plus, urlencode

import anyio
import anyio.to_thread
import httpx
from domo_common.gatekeeper_api import (
    NEVER_A_PLACE,
    BasicLocation,
    Binding,
    HeaderLocation,
    LoginLocation,
    OpenAddress,
    OutRefusalCode,
    OutRequest,
    OutResponse,
    Zone,
)

from domo_gatekeeper.errors import OutRefused
from domo_gatekeeper.providers import binding_allows, default_port
from domo_gatekeeper.reach import IPAddress, Lookup, Reach, Unresolvable, lookup_addresses
from domo_gatekeeper.reach import resolve as resolve_name
from domo_gatekeeper.reach import zone as zone_in
from domo_gatekeeper.relay import HOP_BY_HOP, connection_tokens, header_value
from domo_gatekeeper.scrub import Scrubber, scrub_text
from domo_gatekeeper.sessions import Sessions
from domo_gatekeeper.vault import Vault, secret_parts, split_account

# What every request of a program says: Domo's own page refuses any request that carries it.
VIA = "1.1 domo-out"
MAX_HOPS = 5

ZoneOf = Callable[[IPAddress, int], Zone]

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# What a program never sends itself: what identifies, and what the relay decides.
_NOT_THE_PROGRAMS = HOP_BY_HOP | {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "host",
    "content-length",
    "accept-encoding",
}
# The body is decoded here, and its session cookies are kept: neither header describes it any more.
_NOT_RETURNED = HOP_BY_HOP | {"content-length", "content-encoding"}
_SESSION_HEADERS = frozenset({"set-cookie", "set-cookie2"})
_DECODED = {"gzip": zlib.MAX_WBITS | 16, "deflate": zlib.MAX_WBITS}

# RFC 9110 "token", and what the HTTP stack carries as a header value (printable ASCII and tabs).
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE = re.compile(r"[\x20-\x7e\t]*")


def create_out_ssl_context() -> ssl.SSLContext:
    """The TLS settings of a program's requests: the usual certificate authorities, nothing from
    the environment."""
    return httpx.create_ssl_context(trust_env=False)


@dataclass(frozen=True)
class _Target:
    """An address judged by rules 1 and 2: where the request goes, and how it is named."""

    url: httpx.URL
    host: str
    port: int
    address: IPAddress
    zone: Zone

    @property
    def secure(self) -> bool:
        return self.url.scheme == "https"


@dataclass(frozen=True)
class _Account:
    ref: str
    value: str
    bindings: list[Binding]


@dataclass(frozen=True)
class _Exchange:
    """One request on the wire."""

    target: _Target
    method: str
    headers: dict[str, str]
    body: bytes | None


def _program_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The program's headers by lower-cased name, or ``bad_request`` for one that cannot go."""
    seen: dict[str, str] = {}
    for name, value in headers.items():
        if _HEADER_NAME.fullmatch(name) is None or _HEADER_VALUE.fullmatch(value) is None:
            raise OutRefused("bad_request")
        key = name.lower()
        seen[key] = f"{seen[key]}, {value}" if key in seen else value
    return seen


def _program_body(body_b64: str | None) -> bytes | None:
    if body_b64 is None:
        return None
    try:
        return base64.b64decode(body_b64, validate=True)
    except (binascii.Error, ValueError):
        pass
    raise OutRefused("bad_request")


def _parse(url: str) -> httpx.URL | None:
    try:
        return httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None


def _opened(url: httpx.URL, open: Iterable[OpenAddress]) -> tuple[str, int, list[OpenAddress]]:
    """Rule 1: the host and port of ``url`` and the open addresses they match."""
    if not url.scheme or not url.host:
        raise OutRefused("bad_request")
    if url.scheme not in ("http", "https"):
        raise OutRefused("closed_address")
    if url.userinfo:
        # A secret goes in a request through an account only.
        raise OutRefused("bad_request")
    host, port = url.host, url.port or default_port(url.scheme)
    if not 1 <= port <= 65535:
        raise OutRefused("bad_request")
    matching = [o for o in open if o.host.strip("[]") == host and o.port == port]
    if not matching:
        raise OutRefused("closed_address")
    return host, port, matching


def _without(headers: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    dropped = {name.lower() for name in names}
    return {name: value for name, value in headers.items() if name not in dropped}


def _login_body(location: LoginLocation, user: str, password: str, body: bytes | None) -> bytes:
    """The program's body with the two fields of the login put in, in its encoding."""
    fields = (location.user_field, location.password_field)
    if location.encoding == "form":
        try:
            pairs = parse_qsl((body or b"").decode("utf-8"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            pairs = None
        if pairs is None:
            raise OutRefused("bad_request")
        kept = [(name, value) for name, value in pairs if name not in fields]
        placed = [*kept, (location.user_field, user), (location.password_field, password)]
        return urlencode(placed).encode("ascii")
    try:
        data: Any = json.loads(body or b"{}")
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise OutRefused("bad_request")
    document = cast(dict[str, Any], data)
    document[location.user_field] = user
    document[location.password_field] = password
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _login_fields_as_sent(location: LoginLocation, user: str, password: str) -> list[str]:
    """The two fields of a login as ``_login_body`` writes them, each value alone and with its
    name: in a form, percent-encoded with ``+`` for a space; in JSON, escaped."""
    pairs = ((location.user_field, user), (location.password_field, password))
    if location.encoding == "form":
        return [
            text
            for name, value in pairs
            for text in (quote_plus(value), urlencode([(name, value)]))
        ]
    as_sent: list[str] = []
    for name, value in pairs:
        quoted = json.dumps(value, ensure_ascii=False)
        as_sent += [quoted[1:-1], f"{json.dumps(name, ensure_ascii=False)}: {quoted}"]
    return as_sent


def _codings(response: httpx.Response) -> list[str]:
    return [
        token.strip().lower()
        for value in response.headers.get_list("content-encoding")
        for token in value.split(",")
        if token.strip() and token.strip().lower() != "identity"
    ]


class _Body:
    """The decoded body of an answer, refused (``too_big``) as soon as it would pass ``most``
    bytes: a compressed body is decoded at most one byte beyond what is left."""

    def __init__(self, coding: str | None, most: int) -> None:
        self._coding = coding
        self._most = most
        self._parts: list[bytes] = []
        self._size = 0
        self._inflater = None if coding is None else zlib.decompressobj(_DECODED[coding])
        self._started = False

    def _add(self, data: bytes) -> None:
        self._size += len(data)
        if self._size > self._most:
            raise OutRefused("too_big")
        self._parts.append(data)

    def _inflate(self, data: bytes) -> bytes:
        assert self._inflater is not None
        room = self._most - self._size + 1  # never 0: a max_length of 0 means no limit
        try:
            return self._inflater.decompress(data, room)
        except zlib.error:
            if self._coding != "deflate" or self._started:
                raise
        # "deflate" is sometimes sent without its zlib wrapper.
        self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._inflater.decompress(data, room)

    def feed(self, data: bytes) -> None:
        if self._inflater is None:
            self._add(data)
            return
        decoded = self._inflate(data)
        self._started = True
        self._add(decoded)
        if self._inflater.unconsumed_tail:
            # Only possible once the bound is reached: more than ``most`` bytes.
            raise OutRefused("too_big")

    def end(self) -> bytes:
        if self._inflater is not None:
            self._add(self._inflater.flush())
        return b"".join(self._parts)


def _failure(exc: Exception) -> OutRefusalCode:
    """What a failure of the HTTP stack is, said without anything of its text."""
    return "too_slow" if isinstance(exc, httpx.TimeoutException) else "unreachable"


async def _close(closing: Callable[[], Any]) -> None:
    # Shielded: the close must complete even when it runs because time ran out.
    with anyio.CancelScope(shield=True):
        await closing()


class ProgramRelay:
    """Sends the requests of programs (``send``), with what the gatekeeper knows of where it runs
    (``reach``), its resolver (``lookup``) and the sessions of the accounts.

    ``zone_of`` judges an address at a port; it is ``reach.zone`` on ``reach`` unless given.
    """

    def __init__(
        self,
        *,
        vault: Vault,
        sessions: Sessions,
        reach: Reach,
        ssl_context: ssl.SSLContext,
        lookup: Lookup = lookup_addresses,
        zone_of: ZoneOf | None = None,
    ) -> None:
        self._vault = vault
        self._sessions = sessions
        self._ssl_context = ssl_context
        self._lookup = lookup
        self._zone_of: ZoneOf = zone_of or partial(_reach_zone, reach)

    async def send(self, request: OutRequest) -> OutResponse:
        """Send ``request`` and return its answer; ``OutRefused`` with its code otherwise."""
        try:
            with anyio.fail_after(request.seconds):
                return await self._send(request)
        except TimeoutError:
            pass
        raise OutRefused("too_slow")

    # --- Rules 1 and 2 ----------------------------------------------------------------------------

    async def _judge(self, url: httpx.URL, open: list[OpenAddress]) -> _Target:
        host, port, matching = _opened(url, open)
        try:
            address = await resolve_name(host, lookup=self._lookup)
        except Unresolvable:
            address = None
        if address is None:
            raise OutRefused("unreachable")
        zone = self._zone_of(address, port)
        if zone == "never":
            raise OutRefused("never_reachable")
        if zone == "host" and not any(o.on_host for o in matching):
            raise OutRefused("host_not_opened")
        if zone != "host" and all(o.on_host for o in matching):
            raise OutRefused("closed_address")
        return _Target(url=url, host=host, port=port, address=address, zone=zone)

    async def _judge_hop(self, current: _Target, location: str, open: list[OpenAddress]) -> _Target:
        try:
            url = current.url.join(location)
        except (httpx.InvalidURL, TypeError, ValueError):
            url = None
        if url is None:
            raise OutRefused("bad_redirect")
        target: _Target | None = None
        with suppress(OutRefused):
            target = await self._judge(url, open)
        # A hop stays in the zone of the first address: the core judged that one only, and a
        # program that wants the other address asks for it itself.
        if target is None or target.zone != current.zone:
            raise OutRefused("bad_redirect")
        return target

    # --- Rule 5 -----------------------------------------------------------------------------------

    def _with_account(self, account: _Account, exchange: _Exchange) -> tuple[_Exchange, list[str]]:
        """The exchange with the account placed, and every value exactly as it was placed: the
        answer is scrubbed of them (and so of their encoded forms) besides the secret's parts."""
        target = exchange.target
        if any(
            isinstance(b.location, HeaderLocation) and b.location.name in NEVER_A_PLACE
            for b in account.bindings
        ):
            # Never stored (the model refuses it): a binding never replaces the marker of my
            # tools, the framing of the request or its cookies.
            raise OutRefused("account_not_for_address")
        allowed = [b for b in account.bindings if binding_allows(b, target.host, target.port)]
        if not allowed:
            raise OutRefused("account_not_for_address")
        if not target.secure and target.zone not in ("house", "host"):
            # Judged on the address the name led to, never on the shape of the name.
            raise OutRefused("needs_https")
        headers, body = dict(exchange.headers), exchange.body
        placed: list[str] = []
        parts = split_account(account.value)
        path = target.url.raw_path.split(b"?")[0].decode("ascii")
        for binding in allowed:
            location = binding.location
            if isinstance(location, HeaderLocation):
                value = header_value(account.ref, location.template, account.value)
                headers[location.name] = value
                placed.append(value)
            elif parts is None:
                continue
            elif isinstance(location, BasicLocation):
                pair = base64.b64encode(f"{parts[0]}:{parts[1]}".encode()).decode("ascii")
                headers["authorization"] = f"Basic {pair}"
                placed += [f"Basic {pair}", pair]
            elif exchange.method == "POST" and path == location.path:
                # A login: in the body of a POST to its path, and nowhere else.
                body = _login_body(location, parts[0], parts[1], body)
                headers["content-type"] = (
                    "application/x-www-form-urlencoded"
                    if location.encoding == "form"
                    else "application/json"
                )
                placed += _login_fields_as_sent(location, parts[0], parts[1])
        cookies = self._sessions.cookies_for(
            account.ref, target.host, target.port, path, target.secure
        )
        if cookies is not None:
            headers["cookie"] = cookies
        placed_exchange = _Exchange(
            target=target, method=exchange.method, headers=headers, body=body
        )
        return placed_exchange, placed

    # --- Sending ----------------------------------------------------------------------------------

    @asynccontextmanager
    async def _sent(self, exchange: _Exchange) -> AsyncGenerator[httpx.Response]:
        """Rule 3: one connection, to the judged address, used for this exchange alone."""
        target = exchange.target
        transport = httpx.AsyncHTTPTransport(verify=self._ssl_context, trust_env=False, retries=0)
        client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=None
        )
        try:
            headers = {
                **exchange.headers,
                "host": target.url.netloc.decode("ascii"),
                "accept-encoding": "identity",
            }
            extensions: dict[str, Any] = {}
            if target.secure and target.host != str(target.address):
                extensions["sni_hostname"] = target.host
            request = client.build_request(
                exchange.method,
                target.url.copy_with(host=str(target.address)),
                headers=headers,
                content=exchange.body,
                extensions=extensions,
            )
            response: httpx.Response | None = None
            failure: OutRefusalCode = "unreachable"
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                failure = _failure(exc)
            if response is None:
                raise OutRefused(failure)  # outside the handler: nothing of the error is chained
            try:
                yield response
            finally:
                await _close(response.aclose)
        finally:
            await _close(client.aclose)

    async def _send(self, request: OutRequest) -> OutResponse:
        self._sessions.forget_idle(request.session_idle_seconds)
        given = _program_headers(request.headers)
        program_body = _program_body(request.body_b64)
        url = _parse(request.url)
        if url is None:
            raise OutRefused("bad_request")
        target = await self._judge(url, request.open)
        if request.expect_zone is not None and target.zone != request.expect_zone:
            # The core judged another zone: on this very lookup, which the connection is pinned
            # to, the name leads elsewhere (rule 2).
            raise OutRefused("zone_changed")
        if request.resolve_only:
            return OutResponse(status=0, headers={}, body_b64="", zone=target.zone, hops=0)

        account: _Account | None = None
        secrets: list[str]
        if request.account is not None:
            found = await anyio.to_thread.run_sync(self._reveal, request.account)
            if found is None:
                raise OutRefused("account_not_found")
            account, secrets = found
        else:
            secrets = await anyio.to_thread.run_sync(self._vault.values)

        placed = {
            location.name
            for binding in (account.bindings if account else [])
            if isinstance(location := binding.location, HeaderLocation)
        }
        dropped = _NOT_THE_PROGRAMS | connection_tokens(given.items()) | placed
        program_headers = _without(given, dropped)
        via = given.get("via")
        program_headers["via"] = f"{via}, {VIA}" if via else VIA

        exchange = _Exchange(
            target=target, method=request.method, headers=program_headers, body=program_body
        )
        if account is not None:
            exchange, placed_values = self._with_account(account, exchange)
            secrets = [*secrets, *placed_values]
            await anyio.to_thread.run_sync(self._vault.mark_used, account.ref)

        hops = 0
        while True:
            async with self._sent(exchange) as response:
                if account is not None and hops == 0:
                    # Only the answer to the request that carried the account feeds its session:
                    # a hop carried neither the account nor its cookies.
                    self._sessions.keep(
                        account.ref, exchange.target.host, response.headers.get_list("set-cookie")
                    )
                location = response.headers.get("location")
                if response.status_code not in _REDIRECTS or location is None:
                    return await self._answer(
                        response, exchange, request, target.zone, hops, secrets, account
                    )
            if hops == MAX_HOPS:
                raise OutRefused("bad_redirect")
            hops += 1
            exchange = await self._hop(
                exchange, response.status_code, location, request, program_headers, program_body
            )

    async def _hop(
        self,
        previous: _Exchange,
        status: int,
        location: str,
        request: OutRequest,
        program_headers: dict[str, str],
        program_body: bytes | None,
    ) -> _Exchange:
        """Rule 6: the next request, judged again, without the account, its cookies nor the body
        it placed."""
        target = await self._judge_hop(previous.target, location, request.open)
        method, headers, body = previous.method, dict(program_headers), program_body
        if (status == 303 and method != "HEAD") or (status in (301, 302) and method == "POST"):
            method, body = "GET", None
            headers = _without(headers, ("content-type",))
        return _Exchange(target=target, method=method, headers=headers, body=body)

    def _reveal(self, ref: str) -> tuple[_Account, list[str]] | None:
        """The account ``ref`` and what its answer is scrubbed of (blocking: the vault)."""
        try:
            value, bindings = self._vault.reveal_bound(ref)
        except KeyError:
            return None
        # The global set never holds a user alone: this account's own parts are added here.
        return _Account(ref, value, bindings), [*self._vault.values(), *secret_parts(value)]

    # --- Rule 7 -----------------------------------------------------------------------------------

    async def _answer(
        self,
        response: httpx.Response,
        exchange: _Exchange,
        request: OutRequest,
        zone: Zone,
        hops: int,
        secrets: list[str],
        account: _Account | None,
    ) -> OutResponse:
        codings = _codings(response)
        if len(codings) > 1 or any(coding not in _DECODED for coding in codings):
            raise OutRefused("unreachable")
        declared = response.headers.get("content-length", "")
        if (
            exchange.method != "HEAD"
            and not codings
            and declared.isdecimal()
            and int(declared) > request.most_bytes
        ):
            raise OutRefused("too_big")
        body = _Body(codings[0] if codings else None, request.most_bytes)
        content: bytes | None = None
        failure: OutRefusalCode = "unreachable"  # a body that does not decode
        try:
            async for chunk in response.aiter_raw():
                body.feed(chunk)
            content = body.end()
        except httpx.HTTPError as exc:
            failure = _failure(exc)
        except zlib.error:
            pass
        if content is None:
            raise OutRefused(failure)  # outside the handler: nothing of the error is chained
        # The cookies of this account's session, those just kept included; never another's.
        masked = [*secrets, *(self._sessions.values(account.ref) if account else [])]
        scrubber = Scrubber(masked)
        return OutResponse(
            status=response.status_code,
            headers=_returned_headers(response, masked, account is not None),
            body_b64=base64.b64encode(scrubber.feed(content) + scrubber.flush()).decode("ascii"),
            zone=zone,
            hops=hops,
        )


def _reach_zone(reach: Reach, address: IPAddress, port: int) -> Zone:
    return zone_in(address, reach, port)


def _returned_headers(
    response: httpx.Response, secrets: list[str], with_account: bool
) -> dict[str, str]:
    items = response.headers.multi_items()
    dropped = _NOT_RETURNED | connection_tokens(items)
    if with_account:
        # The session's cookies went to its jar: they are not the program's.
        dropped |= _SESSION_HEADERS
    returned: dict[str, str] = {}
    for name, value in items:
        key = name.lower()
        if key in dropped or scrub_text(key, secrets) != key:
            continue
        masked = scrub_text(value, secrets)
        returned[key] = f"{returned[key]}, {masked}" if key in returned else masked
    return returned
